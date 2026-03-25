# `ingestion-and-memory-refactoring.md` 审阅意见

## 1. 结论

这份设计文档方向基本正确，尤其是它准确瞄准了两个真实问题：

- 长对话/长文档按单条 memory 存储时，召回粒度过粗，细节命中率差。
- 检索延迟的主要长尾不在向量库，而在意图路由阶段。

但从当前仓库实现看，这份方案仍停留在“策略层面正确”，尚未达到“可以低风险落地”的程度。主要问题不是 Chunking、Session 树、语义抽取这些点子本身，而是它没有回答以下关键问题：

- 与现有 `session_end -> TraceSplitter -> TraceStore/Archivist` 管线是什么关系。
- 多轨写入如何保持幂等、去重、回溯一致性。
- Chunk 数量膨胀后，索引成本、检索排序、父子树遍历是否仍然可控。
- “用户无感” 前提下，现有 API 契约、数据契约、失败语义如何保持稳定。

如果直接按文档实施，高概率会出现“两套 Session 摄入体系并存、数据重复写、召回排序失真、延迟改善不稳定”的问题。

## 2. 重大问题

### 2.1 没有处理与现有 Alpha Session 管线的重叠，存在重复建设风险

文档提议在 `MemoryOrchestrator.add` 内引入 `Conversation / Session` 模式，并在其中执行滑窗切分、父子建树和语义抽取。[`docs/design/ingestion-and-memory-refactoring.md:15`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L15) [`docs/design/ingestion-and-memory-refactoring.md:31`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L31)

但仓库里已经存在完整的 Session 后处理链路：

- `session_end()` 会把 transcript 从 `Observer` flush 出来，再交给 `TraceSplitter`。[`src/opencortex/orchestrator.py:1758`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/orchestrator.py#L1758)
- `TraceSplitter` 已经负责“按任务拆分 Session、生成摘要、超长时滑窗处理”。[`src/opencortex/alpha/trace_splitter.py:1`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/alpha/trace_splitter.py#L1)
- `cortex-alpha-design.md` 明确写了“不重写 HierarchicalRetriever / IntentRouter 核心链路”，并把 `Trace Splitter` 作为正式模块。[`docs/cortex-alpha-design.md:31`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/cortex-alpha-design.md#L31) [`docs/cortex-alpha-design.md:90`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/cortex-alpha-design.md#L90)

设计文档没有说明：

- 新方案是替代 `TraceSplitter`，还是补充到 Alpha 管线。
- `Conversation 模式` 是走 `add()` 入口，还是只走 `session_end()` 入口。
- Trace、Session Chunk、语义 memory 三者谁是主事实来源。

这是当前最大的架构缺口。只要这个问题不先定，后续所有实现都会把复杂度放大两倍。

### 2.2 路由规则过于启发式，误判成本被低估

文档建议按 `session_id`、`source_path`、正则特征等方式在 `add()` 内自动推断 ingest mode。[`docs/design/ingestion-and-memory-refactoring.md:23`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L23)

这在概念上简洁，但对当前接口契约并不稳：

- `MemoryOrchestrator.add()` 目前是一个“原子写入一条 Context”的 API，调用方传入什么，底层就按一条上下文去做 URI 生成、层级提炼、向量化、去重、落库。[`src/opencortex/orchestrator.py:661`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/orchestrator.py#L661)
- 仅凭存在 `session_id` 就改走 Conversation 流水线，等于把 `session_id` 从“附加 metadata”提升成“控制写入语义的强信号”，这会改变老调用方语义。
- “看起来像对话” 的正则判断非常脆弱。日志、脚本样例、测试 fixture、chat transcript 文档都可能被误识别为 Conversation。

误判进入复杂流水线不是轻微问题，而是会改变：

- 生成多少条记录。
- URI 和 parent 结构。
- 是否触发异步抽取。
- 写时 dedup 的命中行为。

文档把这个 Resolver 描述成“轻量级判断逻辑”，实际它是一个高影响的数据平面分流器，应该有显式覆盖率和误判回滚设计。

### 2.3 多轨处理没有定义幂等性与一致性模型

文档提出“情景归档轨 + 语义提取轨”并行或异步执行。[`docs/design/ingestion-and-memory-refactoring.md:35`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L35) [`docs/design/ingestion-and-memory-refactoring.md:50`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L50)

但缺失下面这些必须先定义的约束：

- 同一个 Session/Document 重复摄入时，如何避免重复生成父节点和 Chunk 节点。
- 第一轨成功、第二轨失败时，系统把这次写入视为成功、部分成功还是待补偿。
- 第二轨 later retry 时，如何知道它对应的是哪一版 Chunk 树。
- 如果 Chunk 被重新切分，`meta.chunk_uri` 的稳定性如何保证。

当前 `ContextManager.prepare()` 和协议文档都非常强调 `(tenant_id, user_id, session_id, turn_id)` 级别的幂等与隔离。[`src/opencortex/context/manager.py:163`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/context/manager.py#L163) [`docs/memory-context-protocol.md:372`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/memory-context-protocol.md#L372)

而这份新设计只说“记录 `session_id` 和 `chunk_uri` 实现溯源”，没有定义：

- 写入主键
- 任务重试键
- Session revision/version
- 部分成功补偿策略

没有这些，异步多轨一定会在重试、重放、批量导入时出现重复数据和悬挂引用。

### 2.4 忽略了现有写时 dedup 机制会对 Chunk/语义提取造成干扰

`MemoryOrchestrator.add()` 当前默认会对叶子节点做向量级去重，并且对 mergeable category 执行合并写入。[`src/opencortex/orchestrator.py:771`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/orchestrator.py#L771)

在新设计下，这会直接引出几个问题：

- 相邻滑窗 Chunk 高度相似，极可能被 dedup 误判，导致 Session 树残缺。
- 语义提取出的 `preferences`/`entities` 如果跨 Session 语义接近，可能被 merge 进旧记录，破坏“来源到 Chunk”的一对一可追溯关系。
- 父节点 `is_leaf=False` 不走 dedup，但子节点走 dedup，会让树的逻辑结构和物理结构不一致。

文档没有说明 Conversation/Document 模式下：

- 是否默认关闭 dedup。
- 是否改成 `session-local dedup`。
- 是否需要区分“Chunk dedup”和“semantic fact dedup”。

如果不拆开这三类 dedup，最终检索结果很难稳定。

### 2.5 “向量化文本拓展” 的收益被高估，副作用没有评估

文档建议把向量化文本从 `abstract` 扩展到 `abstract + overview + keywords`。[`docs/design/ingestion-and-memory-refactoring.md:65`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L65)

这确实可能提高召回面，但它不是纯收益：

- 当前 `Context.get_vectorization_text()` 只是返回 `self.vectorize.text`，说明 vector text 不是简单字段拼接，而是已有封装入口。[`src/opencortex/core/context.py:132`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/core/context.py#L132)
- `overview` 往往包含高频概述词，容易把 embedding 拉向宽泛主题，稀释精准实体。
- `keywords` 如果由 LLM 生成，质量波动会直接污染 dense embedding，而 lexical 侧对这些词更敏感。
- Dense 与 lexical “字段对齐” 不等于“排序效果对齐”。两个通道的最佳文本配方通常不同。

文档把这一步描述为几乎确定性的收益，但实际需要 A/B 验证：

- `abstract`
- `abstract + keywords`
- `abstract + overview`
- 分字段 embedding / late fusion

否则很可能提升 Recall@K，反而伤害 Top-1 精度。

### 2.6 对 Intent Router 延迟问题的诊断不够闭环

文档把 Intent Router 定性为 8s-22s 的主要瓶颈，并提出 Zero-LLM、瘦身 Prompt、级联、小模型守门、TTL 缓存等优化。[`docs/design/ingestion-and-memory-refactoring.md:69`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L69)

这里有两个问题：

- 当前 `ContextManager.prepare()` 对 `IntentRouter.route()` 已经有 2 秒 timeout，超时直接降级，不会无限等待。[`src/opencortex/context/manager.py:188`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/context/manager.py#L188)
- 现有 `IntentRouter` 也已经有关键词层和 no-recall 直通，并不是每个请求都强依赖 LLM。[`src/opencortex/retrieve/intent_router.py:85`](/Users/hugo/CodeSpace/Work/OpenCortex/src/opencortex/retrieve/intent_router.py#L85)

因此文档里的 8s-22s 更像某个特定链路、特定运行模式或历史观测，而不是当前主分支的稳定事实。设计里缺少：

- 观测样本来自哪个入口，`orchestrator.search()` 还是 `memory_context.prepare()`。
- 超时是 LLM provider 长尾、prompt 过大、网络抖动还是串行调用导致。
- 缓存命中率和关键词短路命中率的现状基线。

没有这组基线，Intent 优化部分容易变成“正确但无证据”的泛化建议。

## 3. 次要问题

### 3.1 Session 树的父节点语义不稳定

文档默认“宏观问题命中父节点，细节问题命中子节点”。[`docs/design/ingestion-and-memory-refactoring.md:44`](/Users/hugo/CodeSpace/Work/OpenCortex/docs/design/ingestion-and-memory-refactoring.md#L44)

但如果父节点摘要由所有 Chunk 汇总生成，它天然更像“多主题混合入口”，容易在 dense 检索中抢占子节点的分数。除非检索阶段加入：

- `is_leaf` 感知
- 父子分桶召回
- 每父节点配额

否则父节点可能因为词覆盖面更广，反而压掉真正需要的证据块。

### 3.2 文档与对话共用一套 Chunking 思路，抽象层次偏粗

长文档通常有显式结构边界，Session 则有轮次、说话人、任务切换、澄清回合。二者都叫“滑窗切分”，但目标完全不同：

- 文档更关注主题完整性和章节边界。
- 对话更关注 turn adjacency、指代链、任务切换点。

把两者并在同一模式族下没问题，但不应共享同一切分策略抽象，否则后面会不断塞例外分支。

### 3.3 缺少成本预算

新方案会增加：

- 每个 Chunk 的 LLM 提炼调用
- Session 父节点汇总调用
- 语义提取调用
- 更多 embedding/upsert 次数

但文档完全没有给出：

- 单个 100 轮 Session 的预期 Chunk 数
- LLM 调用次数上界
- 每次摄入平均新增多少向量
- 回填/补偿重试时的成本控制

没有成本上界，Phase 2/3 很难判断是否适合默认开启。

### 3.4 缺少面向失败模式的数据清理策略

新流水线一旦中途失败，会留下：

- 只有父节点没有子节点
- 只有部分子节点
- 已抽取 semantic memory 但源 chunk 不存在

文档没有定义清理方式，是依赖幂等覆盖、软删除，还是后台 reconciler 修复。

## 4. 建议的修订方向

### 4.1 先明确“新方案与 Alpha 管线”的边界

建议先做一个架构决策，而不是直接进 Phase 1：

- 方案 A：Conversation ingestion 完全复用 `session_end -> TraceSplitter`，新文档只负责把 trace/knowledge 写入现有检索体系。
- 方案 B：弃用 Alpha trace 管线，由 `IngestModeResolver` 统一接管长对话/长文档摄入。
- 方案 C：短期双轨，但必须明确哪条是实验链路、哪条是生产链路，并禁止同一 Session 被两条链路同时写主存储。

如果不先做这个决策，后续所有接口设计都会漂移。

### 4.2 把 `IngestModeResolver` 从“猜测器”改成“显式优先、启发式只做降级”

更稳妥的顺序应是：

1. 明确传入 `ingest_mode` 时严格执行。
2. `batch_store` / `session_end` 这种强语义入口各自绑定固定 pipeline。
3. 只有在通用 `add()` 入口且调用方未声明模式时，才启发式推断。
4. 启发式命中复杂模式时，先打标/观测，不直接扩展成多条写入。

也就是先做“shadow classification”，再做“active routing”。

### 4.3 先定义数据契约，再实现多轨并发

至少要补齐这些字段/规则：

- `source_ingest_id`: 一次原始摄入的稳定 ID
- `source_revision`: 同一源内容的版本号
- `pipeline_stage`: `episodic_parent` / `episodic_chunk` / `semantic_fact`
- `extraction_job_id`: 异步任务幂等键
- `derived_from_uri` 或更稳定的 `source_chunk_id`

同时定义：

- 哪个对象是主记录
- 哪些对象允许重建
- 哪些对象允许 merge
- 哪些对象必须 append-only

### 4.4 把 dedup 策略拆层

建议不要沿用现在的单一 dedup 开关，而是分开设计：

- `chunk_dedup`: 默认关闭，或仅在同一 `source_ingest_id` 内做结构性去重
- `fact_dedup`: 可跨 Session，但必须保留多来源引用，而不是 merge 覆盖
- `parent_dedup`: 基本不需要，优先依赖 ingest id 幂等

### 4.5 检索优化先做最小实验，不要一次绑定多变量

文档 Phase 1 里同时改路由、改向量文本、改 Chunking，这会让收益归因失真。更稳妥的实验顺序是：

1. 只改 `get_vectorization_text()` 或等效入口，验证召回指标变化。
2. 只对导入文档做段落级 chunking，验证 index 膨胀与命中率。
3. 再引入 Session 树。
4. 最后再加语义提取。

否则一旦指标变好或变坏，都不知道是哪一个变量导致。

### 4.6 为 Intent Router 优化补上观测口径

建议在设计文档里补三个最小指标：

- `prepare.intent_ms.p50/p95/p99`
- `keyword_short_circuit_rate`
- `llm_timeout_rate`

并注明数据采样入口和时间范围。否则“8s-22s”这种表述会在后续评审里持续被质疑。

## 5. 推荐的落地顺序

如果目标是低风险推进，我建议不是按当前三期，而是按下面四步：

1. 先补 ADR，明确与 Alpha trace 管线的关系。
2. 做只读/影子版 `IngestModeResolver`，统计误判率，不改变写路径。
3. 对 Document 场景做最小 chunking 实验，验证召回收益和索引成本。
4. 基于 `session_end` 入口演进 Conversation pipeline，而不是直接在通用 `add()` 上隐式扩容语义。

这样更符合当前仓库已有的 Session 生命周期设计，也更容易控制回归面。

## 6. 总评

这份设计抓到了真正的问题，也提出了不少正确方向，但它把“摄入分流、Chunking、层次树、知识抽取、路由加速”五件高耦合的事情揉在了一起，而没有先处理已有系统的边界和数据契约。当前最需要的不是继续补算法细节，而是先把以下三件事写清楚：

- 新方案和 Alpha trace/archivist 体系谁主谁辅。
- 多轨写入的幂等、一致性、失败补偿模型。
- Chunk/semantic fact 在 dedup 和检索排序中的独立策略。

这三点不先落地，后面的实现复杂度和回归风险都会明显高于文档当前预期。
