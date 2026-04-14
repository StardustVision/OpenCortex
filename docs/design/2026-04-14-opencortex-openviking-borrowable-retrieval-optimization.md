# OpenCortex 面向 OpenViking 可借鉴优化的检索设计

> 状态：Draft  
> 日期：2026-04-14  
> 范围：memory retrieval / store-aligned recall path  
> 目标：梳理 OpenViking 可直接借鉴的检索与存储策略，明确 OpenCortex 应如何在不照搬其整体架构的前提下，吸收其高价值部分，修正当前 recall 主链路

## 1. 文档目标

本文档回答四个问题：

1. OpenViking 的检索策略到底是什么，为什么它通常更稳。
2. OpenViking 的 store 是如何支撑其检索策略的，尤其是 `L0/L1` 的生成与使用方式。
3. 当前 OpenCortex 与 OpenViking 的关键差距在哪里。
4. OpenCortex 应该借鉴什么，不该照搬什么，以及一条可落地的演进路径是什么。

本文档聚焦 retrieval 主链路，不讨论完整产品层面的所有能力。

## 2. 执行摘要

结论先行：

- OpenViking 强，不是因为它有一个“特别准的意图分类器”。
- 它的核心优势是：**先找范围，再找内容；先命中对象，再决定是否下钻**。
- 它把全局检索降级成“起点发现”，把真正的检索交给局部层级递归。
- 它的 `L0/L1` 不是展示层，而是检索结构的一部分。

当前 OpenCortex 的主要问题相反：

- 仍然偏向“先做一次较宽的全域搜索，再从结果里反推 anchor / class / scope”
- `probe` 产出的 `anchors` 主要来自已命中记录的附带字段，而不是独立的第一阶段检索结果
- `planner` 在低置信度下会扩大而不是收窄候选空间
- `executor` 和聚合层还会继续膨胀候选池
- store 已开始具备对象化与层级化字段，但检索主链路尚未真正把这些结构作为“主控制面”

因此，OpenCortex 的正确借鉴方向不是“照搬 OpenViking 的目录体系”，而是：

- 借它的 **分层起点定位**
- 借它的 **强范围约束**
- 借它的 **L0/L1 先裁决、L2 按需加载**
- 保留 OpenCortex 当前统一 store 方向
- 再叠加后续的 m_flow 风格锚点扩散

一句话总结：

> OpenViking 解决的是“去哪找”；m_flow 解决的是“围绕哪个锚点扩散”；OpenCortex 应该把这两件事拆开，而不是继续靠一次宽搜后再补救。

## 3. OpenViking 检索策略拆解

以下结论基于本地 OpenViking 源码：

- `openviking/service/search_service.py`
- `openviking/storage/viking_fs.py`
- `openviking/retrieve/hierarchical_retriever.py`
- `openviking/storage/viking_vector_index_backend.py`

### 3.1 入口分流：`find` 与 `search` 是两条不同路径

OpenViking 把检索入口明确拆成两类：

- `find()`：无 session context 的普通语义检索
- `search()`：带 session context 的复杂检索

这意味着它不是所有 query 都先进入一套重型 planner。

这条设计有两个直接收益：

- 普通检索路径不需要承担上下文分析成本
- 复杂上下文检索才允许调用更重的语义分析

### 3.2 session-aware：只有有 session context 时才触发 IntentAnalyzer

在 `viking_fs.search()` 中，只有在存在 `session_summary` 或 `recent_messages` 时，才会调用 `IntentAnalyzer` 生成 `QueryPlan`。

如果没有 session context，则直接构造默认 `TypedQuery`。

这点非常关键，因为它把 LLM 分析限定在真正有收益的场景里，而不是让 recall 热路径默认背上这一成本。

### 3.3 `target_uri` 是一等公民，不是提示词

如果调用时带了 `target_uri`：

- OpenViking 会先推断 `target_context_type`
- 之后生成的查询会强制绑定到 `target_directories`
- 向量过滤器会把它转成真实的路径范围过滤

这说明在 OpenViking 中，“去哪里找”是硬约束，不是语义猜测。

### 3.4 全局检索只负责找起点，不直接决定最终叶子结果

`HierarchicalRetriever` 的关键逻辑不是直接全库搜叶子，而是：

1. 先确定 `root_uris` 或 `target_directories`
2. 再做一次很小的全局 `root` 搜索
3. 取这些结果作为 `starting_points`
4. 从这些起点向下递归检索

而且这个全局搜索还有明显边界：

- 只搜索 `level in [0, 1]`
- `GLOBAL_SEARCH_TOPK = 3`

这说明 OpenViking 中的全局向量搜索，不是“主检索器”，而是“起点发现器”。

### 3.5 真正的主检索是局部递归，而不是全库叶子摊平

真正决定结果的是 `_recursive_search()`：

- 从 `starting_points` 入队
- 对当前 `parent_uri` 的直接孩子执行局部搜索
- 只递归非 `L2` 节点
- 用父子分数传播稳定局部排序
- 满足收敛条件就停

这套策略的本质是：

- 先在对象层 / 目录层判断“是否值得继续”
- 再向更细粒度内容下钻

所以它是典型的：

> object-first / scope-first retrieval

而不是：

> leaf-first / rerank-later retrieval

### 3.6 强收敛与强截断

OpenViking 的检索链路有明确的“别失控”机制：

- 全局起点搜索量很小
- 局部子节点搜索有 `pre_filter_limit`
- 递归有 `MAX_CONVERGENCE_ROUNDS`
- 最终严格 `matched[:limit]`

这一点比任何“换更强 embedding”都重要，因为它决定了 top-k 是否还能代表 top-k。

## 4. OpenViking 的 store 与 `L0/L1` 策略

OpenViking 的检索之所以成立，不只是因为 retriever 写得好，更因为 store 的语义结构天然支持这种搜索方式。

### 4.1 store 形态：文件树是主结构，向量索引是检索加速层

同一个对象 / 目录通常会对应：

```text
{uri}/
  .abstract.md   # L0
  .overview.md   # L1
  content / files / children
```

向量索引中保留这些检索关键字段：

- `uri`
- `parent_uri`
- `level`
- `context_type`

这意味着：

- 文件系统路径表达语义边界
- 向量库负责在这些边界内加速定位

检索不是脱离对象树独立发生的。

### 4.2 OpenViking 的 `L0/L1` 不是单一生成策略

这点很重要。  
OpenViking 并不是所有对象都用同一套 `L0/L1` 抽取逻辑。

不同类型的对象，策略不同：

- live session：规则生成
- archived session：LLM 结构化总结
- memory object：LLM 一次生成三层
- document/resource：自底向上汇总生成

它的真正原则不是“统一模板”，而是：

> 按对象类型选择最便宜且最稳定的 `L0/L1` 生成方式

### 4.3 live session 的 `L0/L1`：规则生成，目的偏导航，不偏事实记忆

当前 session 的写入中：

- `L0 abstract` 是类似 `"{turn_count} turns, starting from '...'"` 的一行说明
- `L1 overview` 是 session 目录结构说明，如 `messages.jsonl`、`history/`、访问路径等

这说明这层 `L0/L1` 主要服务于 session 目录导航，而不是高精度事实召回。

因此，这套策略可以作为“会话容器描述层”参考，但不适合作为 OpenCortex 记忆对象召回的主证据生成方式。

### 4.4 archived session 的 `L0/L1`：先生成高质量 `L1`，再从中抽 `L0`

对 archive session，OpenViking 会：

1. 让 LLM 生成结构化 `Session Summary`
2. 把这个结构化 summary 作为 `L1 overview`
3. 再从 overview 中抽一行摘要作为 `L0 abstract`

所以 archive 的策略是：

> `L1` 主导，`L0` 从 `L1` 派生

这非常适合“对话阶段性归档”这种对象，因为它要求：

- `L1` 有结构
- `L0` 足够短
- 两者语义一致

### 4.5 memory object 的 `L0/L1/L2`：一次同源生成

OpenViking 的 memory extraction prompt 会直接要求 LLM 输出：

- `category`
- `abstract`（L0）
- `overview`（L1）
- `content`（L2）

而且 prompt 明确规定：

- `abstract` 应是便于 merge 和检索的一行摘要
- `overview` 是带结构的中层摘要
- `content` 是完整叙事或完整经验

更新时也不是只改某一层，而是通过 `memory_merge_bundle` 一次性重新生成完整的 `L0/L1/L2`。

因此它的 memory object 策略是：

> 三层同源生成，同步更新，避免层间语义漂移

### 4.6 document/resource 的 `L0/L1`：自底向上生成

目录对象的生成策略是：

1. 先为每个文件生成 summary
2. 收集子目录的 `L0 abstract`
3. 用文件 summary 和子目录 abstract 生成本目录 `L1 overview`
4. 再从 overview 中提取 `L0 abstract`

也就是说：

- 叶子先摘要
- 父节点后汇总
- 父节点的 `L1` 是真正的语义概览
- 父节点的 `L0` 则是从该概览中压缩得到

这是一个非常稳定的文档层级写法，因为父节点摘要不需要再直接吃完整原文，而是吃已经规范化过的孩子摘要。

### 4.7 OpenViking 的关键启示

OpenViking 在 store 层最值得借鉴的不是“目录很多”，而是这三点：

1. **对象层级是主结构，不是附加 metadata**
2. **`L0/L1` 必须和对象身份绑定，而不是临时组装**
3. **`L0/L1` 生成方式应随对象类型变化，而不是统一强行模板化**

## 5. 当前 OpenCortex 与 OpenViking 的核心差距

以下结论基于当前 OpenCortex 源码：

- `src/opencortex/intent/probe.py`
- `src/opencortex/intent/planner.py`
- `src/opencortex/intent/executor.py`
- `src/opencortex/orchestrator.py`
- `src/opencortex/memory/mappers.py`

### 5.1 OpenCortex 现在有 `probe`，但还不是 OpenViking 风格的“起点定位器”

当前 `probe` 会做一次统一的 `storage.search(...)`，然后从命中 record 中抽出：

- `candidate_entries`
- `anchor_hits`
- `top_score`
- `score_gap`

问题在于：

- 这不是 “L0 object probe + anchor probe” 双探针
- 而是一次统一预搜，再从结果里顺手带出 anchor

所以它更接近：

> cheap broad presearch

而不是：

> scoped starting-point locator

### 5.2 当前 `anchors` 主要是命中记录的副产物，不是独立的第一阶段检索结果

当前 `anchor_hits` 的来源是：

- probe 命中 record
- record 上的 `abstract_json` / `structured_slots` / `anchor_hits`

也就是说流程是：

`query -> 命中 record -> record 带出 anchor`

而不是：

`query -> 独立 anchor 检索 -> 锚点反推 object/scope`

因此，如果第一跳 record 命偏了，后续 anchor 也会偏。

### 5.3 当前 scope 还不够硬

OpenCortex 当前 `_build_search_filter()` 主要处理的是：

- tenant
- user
- project
- scope
- category

但 conversation / session / object 范围并没有成为 recall 主路径中的默认硬约束。

这与 OpenViking 的 `target_uri -> PathScope` 有本质区别。

### 5.4 当前 `planner` 在低置信度时倾向于“放大”

当前 planner 的设计中：

- 低 confidence 会抬高 `recall_budget`
- 低 confidence 会抬高 `association_budget`
- 低 confidence 更容易触发 rerank 或更深检索

这本质上是：

> 不确定时搜更广

而 OpenViking 的策略更像：

> 不确定时先收在对象层，再决定是否下钻

### 5.5 当前 `executor` 与聚合层会继续膨胀候选池

当前执行链路存在两个问题：

1. 单 query 候选池会被放大到 `limit * 4` 甚至 `limit * 8`
2. 多路查询聚合后，只做去重，不重新严格按 `limit` 裁切

这使得 `top_k=10` 不再真正等价于“系统只保留最好的 10 个候选”。

### 5.6 当前 store 已经开始对象化，但检索尚未真正 object-first

OpenCortex 当前其实已经有不少 OpenViking-like 基础能力：

- `abstract_json`
- `memory_kind`
- `parent_uri`
- `session_id`
- `is_leaf`
- `structured_slots`

也就是说，store 已经不是纯文本 blob。

但 retrieval 主路径还没有像 OpenViking 那样：

- 先用对象层 / 父节点层裁决
- 再选择性地深入叶子层

当前仍然更多是在 leaf pool 上搜索，然后再回头读取结构。

## 6. 哪些可以直接借鉴

### 6.1 入口分流

OpenCortex 应明确区分：

- 无上下文 / 无会话约束的普通 `find`
- 有 conversation/session 上下文的 `search`

不是所有 query 都默认走同一 recall planning 路径。

### 6.2 probe 必须从“统一预搜”升级为“起点定位器”

建议把 Phase 1 probe 重构为两路：

1. `object_probe`
   - 仅搜索对象级 `L0`
   - 仅输出候选对象 / session / parent

2. `anchor_probe`
   - 仅搜索 anchor entries
   - 输出 `time/entity/topic/preference/constraint`

planner 再把这两路结果汇总。

### 6.3 范围必须成为硬约束

应引入与 OpenViking `PathScope` 同级别的重要性：

- session scope
- object scope
- source_doc_id / section lineage

这些都应在第一阶段进入过滤器，而不是仅在 rerank 时体现。

在当前 v1 设计中，conversation recall 的会话隔离键只使用 `session_id`。
不额外引入 `conversation_id`，除非未来产品语义要求“一段 conversation 跨多个 OpenCortex session”。

### 6.4 `L0/L1` 必须成为主检索控制面

当前 OpenCortex 需要把 `L0/L1` 从“内容层概念”提升为“检索层概念”：

- `L0` 用于起点定位
- `L1` 用于仲裁和 sufficiency 判断
- `L2` 仅用于按需 hydration

### 6.5 强截断与强收敛

OpenCortex 应引入与 OpenViking 同级别的硬规则：

- probe candidate cap
- object retrieval candidate cap
- cone expansion cap
- rerank candidate cap
- aggregate 后二次 `cap(limit)`

## 7. 哪些不该照搬

### 7.1 不应照搬 OpenViking 的多目录语义耦合

OpenViking 的 category 路径组织适合它自己的 URI-first 架构，但不适合 OpenCortex 当前统一 store 方向。

OpenCortex 不应为了借鉴其检索策略，就回退成“检索逻辑强绑定目录分类”的系统。

### 7.2 不应照搬 live session 的规则 `L0/L1`

OpenViking 当前 session 的 `.abstract.md / .overview.md` 更像容器说明，不适合作为高质量记忆召回证据。

对 OpenCortex 来说：

- conversation 的归档对象可以学 archive summary 路线
- durable memory object 不应学 live session 规则摘要路线

### 7.3 不应把 OpenViking 的意图分析当作核心优势照搬

OpenViking 的价值不在“LLM 意图分析本身特别强”，而在于：

- 它只在值得时才调用
- 输出会被真实结构边界约束

OpenCortex 如果只学它的 `IntentAnalyzer` 风格，却不学范围约束与层级递归，收益会很有限。

## 8. 面向 OpenCortex 的推荐融合方案

### 8.1 总体方向

推荐的最终主链路应是：

```text
query
  -> scoped object_probe (L0)
  -> scoped anchor_probe
  -> planner(probe_result)
  -> constrained object retrieval
  -> optional cone expansion
  -> L1 sufficiency check
  -> optional L2 hydration
```

其核心原则是：

- Phase 1 解决“去哪找”
- Phase 2 解决“找多深”
- Phase 3 解决“围绕哪个对象/锚点扩散”

### 8.2 retrieval 的职责重划分

#### Phase 1: probe

职责：

- 只负责找起点
- 不做大语义分类
- 不放大候选池

输出建议：

- `candidate_objects`
- `anchor_hits`
- `object_confidence`
- `scope_confidence`

#### Phase 2: planner

职责：

- 判断 `L0` 是否足够
- 是否需要 `L1`
- 是否需要 `L2`
- 是否需要 cone
- 下发硬过滤条件

不再承担“解释整句 query 属于哪个业务意图”的职责。

#### Phase 3: executor

职责：

- 在已缩小的对象范围内做检索
- 只对少量候选做 cone
- 做严格上限控制

### 8.3 store 的最小补强方向

OpenCortex 不需要回退成 OpenViking 式多目录系统，但需要让当前统一 store 更像“对象树”：

- 每个 durable object 都有稳定的 `L0/L1/L2`
- `.abstract.json` 必须是机器可消费的一致结构
- `parent_uri / session_id / source_doc_id / msg_range` 应成为检索级字段
- anchor entries 必须可独立检索，但生命周期仍从属于 object

### 8.4 三种 input 的 `L0/L1` 生成建议

#### conversation

- `immediate` object：每次 commit 后立即抽取对象和 anchors，优先保证短延迟可检索
- `merged` conversation object：应学习 OpenViking archive 路线
  - 先生成结构化 `L1`
  - 再导出 `L0`
- `final` object：可选，只在额外收敛确实提升检索质量时生成
- `immediate -> merged -> final` 必须是 supersede 关系，而不是三层长期并存
  - 新阶段对象提交后，旧阶段对象及其 anchors 应退出检索面

不建议使用 live session 那种规则型目录摘要作为 durable memory 的主表达。

#### memory

- 直接对象写入应学习 OpenViking memory bundle 路线
  - 同一次生成 `L0/L1/L2`
  - 更新时同源重写

#### document

- 学习 OpenViking bottom-up 路线
  - 叶子先 summary
  - 父对象生成 `L1`
  - 再从 `L1` 导出 `L0`

### 8.5 与 m_flow 的接口预留

OpenViking 借来的是“起点定位与层级缩域”。  
m_flow 后续叠加的是“锚点扩散与证据链排序”。

因此 OpenCortex 应预留这两个接口：

- `anchor_probe` 输出稳定 anchor ids / types
- executor 能基于 object + anchor 做 bounded neighborhood expansion

这样 OpenViking 和 m_flow 的优势就不会打架，而是前后衔接。

## 9. 实施优先级

按收益与风险排序，建议顺序如下：

### P0

- 为 conversation recall 注入真实的 `session_id` 硬过滤
- probe 只搜对象级 `L0`
- aggregate 后重新严格 `cap(limit)`

### P1

- 引入独立 `anchor_probe`
- planner 输出硬过滤条件，而不是只输出预算
- `L1 sufficiency` 正式进入 planner / executor 协议

### P2

- object-first cone expansion
- 基于 anchor / relation / time adjacency 的 typed neighborhood
- 对 `document` 和 `conversation` 分别细化 `L0/L1` 写法

## 10. 非目标

本阶段不做这些事情：

- 不照搬 OpenViking 的完整目录组织与分类体系
- 不把 OpenCortex 改造成 URI-first 文件系统产品
- 不在本阶段引入训练型 intent classifier 作为主依赖
- 不把 anchors 升级成独立 durable memory lifecycle 单元

## 11. 最终结论

OpenViking 最值得借鉴的，不是某个单独模块，而是一整套工程取向：

- 路径 / 对象范围先于大规模语义推断
- `L0/L1` 先于 `L2`
- 局部递归先于全局叶子摊平
- 强约束先于大候选池补救

对 OpenCortex 来说，最正确的借鉴方式不是“变成另一个 OpenViking”，而是：

- 保持当前统一 store 方向
- 把 store 中已经存在的对象化、层级化字段真正接入检索主链路
- 让 `probe -> planner -> executor` 变成一条真正的 object-first recall path

如果这一步做对了，OpenCortex 再叠加 m_flow 风格的锚点扩散，才会真正具备“快、准、狠”的基础。

## 12. 参考代码

### OpenViking

- `openviking/service/search_service.py`
- `openviking/storage/viking_fs.py`
- `openviking/retrieve/hierarchical_retriever.py`
- `openviking/storage/viking_vector_index_backend.py`
- `openviking/session/session.py`
- `openviking/session/memory_extractor.py`
- `openviking/storage/queuefs/semantic_processor.py`
- `openviking/utils/embedding_utils.py`
- `openviking/prompts/templates/compression/memory_extraction.yaml`
- `openviking/prompts/templates/compression/memory_merge_bundle.yaml`
- `openviking/prompts/templates/compression/structured_summary.yaml`
- `openviking/prompts/templates/semantic/overview_generation.yaml`

### OpenCortex

- `src/opencortex/intent/probe.py`
- `src/opencortex/intent/planner.py`
- `src/opencortex/intent/executor.py`
- `src/opencortex/orchestrator.py`
- `src/opencortex/memory/mappers.py`
- `src/opencortex/memory/domain.py`
