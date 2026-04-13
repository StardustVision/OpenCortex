# OpenCortex 技术设计文档

> 版本：draft v1
> 更新：2026-04-06

## 1. 设计目标

OpenCortex 的技术目标，是构建一套面向 Agent 的长程记忆架构，使系统能够：

- 存储原始经历与稳定知识
- 以低 token 成本召回上下文
- 同时基于语义相似和结构联想进行检索
- 在 recall 后改变未来记忆状态
- 对低价值、过时、被替代内容执行代谢
- 将重复经验巩固为更高阶抽象

## 1.1 状态说明

本文同时覆盖三种状态：

- `[已实现]`：当前代码中已有明确对应实现
- `[部分实现]`：已有相关能力，但尚未按本文描述的统一模块收敛
- `[设计中]`：当前主要用于架构定义，尚未形成完整实现

## 2. 上位标准

OpenCortex 采用 `Continuum Memory Architectures (CMA)` 作为顶层行为标准。

因此，这套系统必须满足：

- `Persistence`
记忆必须跨 session 持续存在，而不是依赖重放历史消息。

- `Selective Retention`
不是所有内容都永久保持同等可达性；系统必须选择性保留高价值内容。

- `Retrieval-Driven Mutation`
Recall 不是只读行为，而应改变未来记忆状态。

- `Associative Routing`
检索必须能够沿结构化线索传播，而不是只做平面相似度匹配。

- `Temporal Continuity`
系统必须保留时间邻接关系与过程连续性。

- `Consolidation and Abstraction`
原始经历必须逐步沉淀为知识与技能。

## 3. 系统分层

| 层级 | 作用 | 核心机制 | 状态 |
| --- | --- | --- |
| 认知标准层 | 定义长期记忆系统必须具备的行为 | Continuum Memory Architectures | 概念性 |
| 生命周期层 | 管理 memory 与 trace 的代谢 | Autophagy | 设计中 |
| 生命周期子机制 | Autophagy 内部的高精度单条处理 | Chaperone-Mediated Autophagy 启发式管线 | 设计中 |
| 存储基底层 | 持久化分层记忆表示 | CortexFS + 向量存储 | 已实现 |
| 召回层 | 完成语义召回与结构召回 | dense + lexical + rerank + cone retrieval | 已实现 |
| 轨迹层 | 维护过程连续性 | transcript + trace store | 已实现 |
| 巩固层 | 产出知识抽象 | archivist + knowledge extraction | 已实现 |
| 技能层 | 外化可复用操作能力 | OpenSpace 对齐 SkillEngine | 已实现 |

注：`Chaperone-Mediated Autophagy` 在这里不是与 `Autophagy` 平行的顶层层级，而是 `Autophagy` 内部的一条设计中子机制。

## 4. 核心数据对象

OpenCortex 的主要数据对象有三类：

### 4.1 Memory

Memory 是原始或半结构化上下文资产，包括：

- preference
- profile
- constraint
- entity fact
- event
- document fragment

### 4.2 Trace

Trace 是按时间组织的过程性记忆，包括：

- session transcript 切片
- task episode
- tool execution history
- failure/fix chain

### 4.3 Knowledge 与 Skill

Knowledge 与 Skill 是下游抽象产物，包括：

- belief
- rule
- SOP
- negative rule
- workflow
- skill

其中，memory 与 trace 是生命周期系统直接管理的输入；knowledge 与 skill 是下游巩固和抽象的输出。

## 5. 三层存储基底 `[已实现]`

OpenCortex 采用 `CortexFS` 三层表示来承载记忆资产。该设计在历史上受 OpenViking 启发，但在当前代码与文档体系中，规范名称应统一为 `CortexFS`：

- `L0`
极短摘要，用于低成本索引和快速预览。

- `L1`
结构化概述，用于大多数 recall 场景。

- `L2`
完整内容，用于审计、深查与再抽象。

三层结构的意义在于：

- 默认以低 token 成本完成 recall
- 允许按需逐层展开细节
- 支持 `L2 -> L1 -> L0` 的渐进式压缩
- 保证在抽象过程中保留可追溯原文

## 5.1 三模式写入 `[已实现]`

当前写入入口由 `IngestModeResolver` 决定内容进入哪条处理路径：

- `memory`
- `document`
- `conversation`

其中：

- `memory` 走标准记忆写入路径
- `document` 走解析器与 chunking 路径
- `conversation` 走对话即时写入与会话后合并路径

## 5.2 Conversation 双层写入 `[已实现]`

在 `conversation` 模式下，当前系统采用两层写入：

- `immediate`
单条消息即时写入，优先保证可立即召回

- `merged`
后续按窗口或阶段对即时记录做合并和替换

这是一条当前已实现的对话记忆路径，与后文设计中的统一生命周期总线不是同一概念。

## 6. 召回栈

OpenCortex 的召回栈由两部分组成：

- 平面语义召回
- 联想式结构增强

### 6.1 平面召回 `[已实现]`

系统首先通过常规信号获取一个较小的初始候选集：

- dense similarity
- sparse / lexical matching
- rerank calibration
- type/source prior
- freshness / reward

这一步负责把候选空间快速缩小。

### 6.2 Recall 入口路由 `[已实现]`

Recall 的入口由 `IntentRouter` 控制，当前实现包含三层分析：

- keyword extraction
- LLM semantic classification
- memory trigger

它决定：

- 是否需要 recall
- `top_k`
- `detail_level`
- `time_scope`
- trigger 扩展查询

### 6.3 锥形检索 `[已实现]`

锥形检索是受 m_flow 启发的联想式召回增强机制。

它的基本原理是：

`先从 query 缩出少量锚点，再沿实体共现邻域向外扩散，最后按路径成本重新收敛答案。`

它不是向量检索的替代品，也不是全图知识图谱推理器，而是一种局部、受约束、面向 recall 质量优化的路径传播机制。当前实现依赖的是 `entity -> memory_ids` 倒排索引与实体共现传播，不包含显式关系边。

### 6.4 锥形检索算法

锥形检索的执行流程是：

1. 先做普通召回，得到一小批粗候选。
2. 从 query 与粗候选中提取 anchor entities。
3. 通过 `entity -> memory_ids` 倒排索引，将实体相关记忆拉入候选前沿。
4. 对候选计算路径成本，而不再只依赖文本相似度。
5. 将 `cone_score` 与原始 retrieval / rerank 分数融合。

核心差异在于：

- 平面召回问的是：`哪条内容最像 query？`
- 锥形检索问的是：`哪条内容与 query 通过最短、最可靠的实体共现传播路径相连？`

### 6.5 路径成本

OpenCortex 用路径成本来评估锥形候选：

`cone_score(candidate) = - min(path_cost(query -> anchors -> candidate))`

路径成本由以下因素构成：

- query 到 anchor 的语义距离
- hop 数量
- 边的可靠性
- 泛匹配惩罚
- 直接实体命中偏好

原则是：

`路径越短、越直接、越具体，候选得分越高。`

说明：这是概念公式。当前实现中还叠加了直接命中惩罚、query entity 命中时的 hop 缩减等启发式规则，最终产出的是用于融合的 `cone bonus` 信号，而不是单一理论公式的直接实现。

## 7. Autophagy 生命周期层 `[设计中]`

在 OpenCortex 中，Autophagy 是从细胞自噬机制中拆解后引入的工程层，而不是生物学过程的直接模拟。

它负责管理 memory 与 trace 的代谢，具体包括：

- 压力感知
- 打标
- 路由
- 隔离
- 压缩
- 合并
- 归档
- 遗忘
- 回收
- 稳态维持

它在系统中的核心职责是：

`执行选择性保留、检索驱动变异和长期稳态控制。`

## 8. Chaperone-Mediated Autophagy 启发式管线 `[设计中]`

在 Autophagy 内部，OpenCortex 进一步引入 `Chaperone-Mediated Autophagy` 的机制启发，用于高精度、单条级别的处理。

这里引入的不是生物术语本身，而是几条工程原则：

- 先识别，再处理
- 由 escort / chaperone 层决定流向
- 通过 gate 控制准入和吞吐
- 将对象送往特定下游处理器
- 优先回收价值，而不是简单删除

### 8.1 生物学到工程机制的映射

| 生物学概念 | OpenCortex 对应机制 |
| --- | --- |
| stress sensing | storage pressure、recall noise、duplication、contradiction、latency pressure |
| motif exposure | duplicate、stale、superseded、conflicting 等 metabolic tag |
| chaperone recognition | `MemoryChaperone` 分类与路由器（planned） |
| LAMP2A gate | `MetabolicGate` 准入控制器（planned） |
| translocation | dispatch 到 compressor、merger、archiver、forgetter、consolidator（planned） |
| degradation + recycling | `L2 -> L1/L0`、trace -> gist、cluster -> knowledge/skill candidate |
| homeostasis | recall 质量与记忆表面稳定性控制 |

### 8.2 适用对象

这条精细代谢管线适合处理：

- 冲突或被新证据覆盖的事实
- 被重复误召回的噪声条目
- 不应立即删除、但需要谨慎降级的高价值记忆
- 已被高阶知识吸收的 trace
- 证据混杂、需要先隔离再决定动作的对象

## 9. 生命周期事件 `[设计中]`

OpenCortex 需要一条面向 memory 与 trace 的生命周期总线，接收如下事件：

- `memory_written`
- `trace_written`
- `memory_recalled`
- `feedback_received`
- `session_closed`
- `background_tick`
- `consolidation_completed`
- `contradiction_detected`

触发模式采用混合式：

- 轻量动作同步执行
- 重量动作异步执行

## 10. 生命周期状态机 `[设计中]`

Memory 与 trace 统一走如下状态：

- `captured`
- `active`
- `reinforced`
- `competing`
- `tagged`
- `quarantined`
- `compressed`
- `consolidated`
- `archived`
- `forgotten`

### 10.1 状态含义

- `captured`
刚写入，尚未完成代谢评估。

- `active`
正常参与 recall。

- `reinforced`
因 recall/citation/feedback 被强化。

- `competing`
与更新证据或更强答案发生竞争。

- `tagged`
被生命周期层识别为需要进一步处理。

- `quarantined`
暂时从主 recall 面隔离。

- `compressed`
已降级为更低成本表示。

- `consolidated`
已被抽象进更高阶产物。

- `archived`
仍保留，但默认不进入主 recall 面。

- `forgotten`
被逻辑删除或物理淘汰。

## 11. 核心流水线

### 11.1 写入后稳定化 `[部分实现]`

触发事件：

- `memory_written`
- `trace_written`

动作：

- dedup 检查
- entity 提取
- temporal edge 建立
- 初始 activation 设置
- 进入 `active`

说明：当前代码中，`dedup` 是写入路径中的同步检查，而不是事件总线驱动的异步后处理。这里描述的是未来统一生命周期总线下的归一化视角。

### 11.2 检索驱动变异 `[部分实现]`

触发事件：

- `memory_recalled`

动作：

- 强化被引用命中的条目
- 抑制重复 near-miss 竞争项
- 更新访问和 freshness 信号
- 在更优答案稳定胜出时标记 superseded

这条流水线直接实现 `Retrieval-Driven Mutation`。

当前系统已经存在 reward、accessed_at、protect 等相关机制，但尚未收敛成统一的 `Autophagy` 变异模块。

### 11.3 反馈修正 `[部分实现]`

触发事件：

- `feedback_received`

动作：

- 更新 reward 与 confidence
- 将噪声条目标记为 quarantine 候选
- 将多次负反馈条目升级为 archive / forget 候选

### 11.4 会话结束巩固 `[已实现]`

触发事件：

- `session_closed`

动作：

- transcript 切分为 trace
- trace 聚类为 episode
- 生成 gist
- 提名 belief、rule、SOP、skill 候选

### 11.5 后台代谢 `[设计中 / 部分实现]`

触发事件：

- `background_tick`

动作：

- 归档陈旧 event
- 合并重复记录
- 压缩长尾 trace
- 降级已被吸收的细节
- 将高价值模式回收到更高阶抽象

## 12. 决策信号 `[设计中 / 部分实现]`

每条 memory 或 trace 应维护如下生命周期信号：

- `activation_score`
- `stability_score`
- `recall_hits`
- `negative_feedback_count`
- `duplicate_density`
- `contradiction_score`
- `consolidation_potential`
- `superseded_by`
- `last_recalled_at`
- `last_mutated_at`
- `metabolic_tags`
- `metabolic_state`

这些信号优先支持规则驱动决策，仅在必要时再调用更重的模型判断。

当前已明确存在或可近似映射到实现中的信号主要包括：

- `reward_score`
- `accessed_at`
- `active_count`
- `protected`

其余信号属于设计中的统一化目标。

## 13. 巩固与技能提炼 `[已实现]`

巩固层负责把重复经验变成更稳定的知识产物：

- 重复事实 -> belief
- 成功模式 -> rule / SOP
- 重复 workflow -> skill candidate
- failure/fix cycle -> negative rule / repair pattern

SkillEngine 作为下游抽象层保持独立。它消费巩固结果，但不接管原始 memory 生命周期。

## 14. 身份隔离与 Agent 接入 `[已实现]`

OpenCortex 当前已经具备两项关键系统基础：

- 基于 JWT Bearer token 的多租户身份隔离，从 claims 中提取 `tid / uid`
- 基于 MCP 的 Agent 接入协议，通过 `memory_context` 等工具完成 prepare / commit / end 生命周期

这两项能力决定了系统既是架构层记忆系统，也是可直接接入 Agent Runtime 的服务。

## 15. 可观测性 `[部分实现]`

Autophagy 和 recall 决策必须具备 explain 能力。

每次生命周期动作至少应回答：

- 为什么这个对象被打标？
- 为什么被路由到这条处理路径？
- 为什么本轮被 gate 放行？
- 为什么被压缩、归档或遗忘？
- 最终回收出了什么高阶产物？

建议指标：

- active memory count
- archive ratio
- compression ratio
- duplicate collapse rate
- consolidation yield
- false forget rate
- recall precision delta
- latency overhead

## 16. 非目标

这套架构不试图：

- 用图算法完全替代原有 recall 排序
- 让生物学术语直接支配实现细节
- 让 SkillEngine 接管整个 memory 生命周期
- 默认在 hot path 中执行重 LLM 处理
- 把所有历史碎片永久保留为同等可达对象

## 17. 最终技术定义

OpenCortex 是一套符合 CMA 标准的长程 Agent 记忆架构。它以 `CortexFS` 三层存储承载持久记忆，以 `m_flow` 风格锥形检索增强联想召回，以 `Autophagy` 生命周期层作为设计中的统一代谢目标，并通过已实现的 trace、knowledge、skill 子系统将重复经验沉淀为知识与技能，同时保留时间连续性与来源可追溯性。
