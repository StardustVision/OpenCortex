# OpenCortex Autophagy Kernel Phase 2 设计

> 日期：2026-04-06
> 状态：Draft v1
> 范围：Autophagy Kernel Phase 2
> 目标：在现有 Recall Planner Phase 1 基础上，落地完整 `Autophagy` 内核的数据面、状态面与主闭环

## 1. 目标

本阶段定义并实现 `Autophagy Kernel` 的完整内核边界，不再把 cognition 生命周期散落在 `orchestrator`、`context manager`、`retrieve` 与 `alpha` 模块中。

本阶段的目标是：

- 为 `memory / trace` 建立独立的 `cognitive_state`
- 以门面 + 子模块方式落地 `Autophagy Kernel`
- 把 recall 后 mutation、巩固候选生成、认知代谢统一收口到 `Autophagy`
- 保持 `Knowledge` 与 `Skill` 独立主权
- 为后续 `Knowledge Governance Layer` 和全量性能测试打下稳定接口

## 2. 非目标

本阶段不做以下事情：

- 不重写现有 `KnowledgeStore` 为完整治理系统
- 不把 `SkillEngine` 并回认知体系
- 不把所有现有 metadata 字段一次性删除
- 不要求首版即用 Rust 改写全部内核逻辑
- 不在本 spec 中展开 `Knowledge Governance Layer` 的完整实现

## 3. 架构主张

本阶段采用以下结构：

- `AutophagyKernel`：认知门面
- `CognitiveStateStore`：认知状态真相源
- `CognitiveStateManager`：状态初始化与确定性迁移
- `RecallMutationEngine`：召回后认知变更
- `ConsolidationGate`：认知到知识的唯一合法出口
- `CognitiveMetabolismController`：长期认知代谢调节

系统边界如下：

- `Autophagy` 只拥有 cognition
- `Knowledge Governance` 只拥有 knowledge
- `SkillEngine` 独立存在，不进入认知状态机
- `Recall Infrastructure` 只提供召回能力，不决定认知命运

## 4. 唯一合法桥

跨层通信只允许通过候选与反馈对象，不允许跨层直接改写状态：

- `Autophagy -> Knowledge`：`ConsolidationCandidate`
- `Knowledge -> Autophagy`：`GovernanceFeedback`
- `Skill -> Knowledge`：`SkillEvidence` 或 `SkillFeedback`

硬约束：

- `Skill` 不得直接修改 `cognitive_state`
- `Knowledge` 不得直接修改 memory payload
- `Recall Infrastructure` 不得直接执行 reinforce、quarantine、archive 等认知决策

## 5. 主数据面

本阶段引入三个并行数据面：

### 5.1 内容面

现有业务对象继续留在原 collection：

- `context` / `memory`
- `trace`
- `knowledge`

内容面负责保存对象内容，不再承载完整认知真相。

### 5.2 认知状态面

新增独立 `cognitive_state` collection，作为认知真相源。

该对象只表达：

- 它现在在认知系统里处于什么状态
- 为什么处于该状态
- 接下来允许什么动作

### 5.3 候选面

新增短生命周期候选对象：

- `ConsolidationCandidate`

它是桥接物，不是 `knowledge` 本体。

## 6. Cognitive State 模型

`cognitive_state` 不使用单一大枚举，而采用正交三轴状态。

### 6.1 基础字段

- `state_id`
- `owner_type`
- `owner_id`
- `tenant_id`
- `user_id`
- `project_id`
- `activation_score`
- `stability_score`
- `risk_score`
- `novelty_score`
- `evidence_residual_score`
- `last_accessed_at`
- `access_count`
- `last_reinforced_at`
- `last_penalized_at`
- `last_mutation_at`
- `last_mutation_reason`
- `last_mutation_source`
- `version`

### 6.2 Lifecycle 轴

描述对象的生命阶段：

- `active`
- `compressed`
- `archived`
- `forgotten`

### 6.3 Exposure 轴

描述对象在认知活动中的可暴露状态：

- `open`
- `guarded`
- `quarantined`
- `contested`

### 6.4 Consolidation 轴

描述对象与知识层的关系进度：

- `none`
- `candidate`
- `submitted`
- `accepted`
- `rejected`
- `expired`

### 6.5 设计约束

- `forgotten` 是逻辑终态，不等于立即物理删除
- `quarantined` 优先于 `open / guarded`
- `accepted / rejected` 只描述巩固结果，不直接判定 memory 生死
- `activation_score` 必须存在上限与回落机制，避免富者愈富

## 7. 内核模块划分

### 7.1 AutophagyKernel

统一门面，负责编排，不承载具体规则。

### 7.2 CognitiveStateStore

职责：

- 持久化 `cognitive_state`
- 提供按 `owner_id / tenant / project / state axis` 查询
- 提供乐观并发更新

### 7.3 CognitiveStateManager

职责：

- ingest 时初始化状态
- 执行确定性状态迁移
- 持久化 mutation 结果

### 7.4 RecallMutationEngine

职责：

- 接收一次 recall outcome
- 计算 reinforce / penalize / guard / quarantine / contest
- 输出结构化状态更新与候选结果

### 7.5 ConsolidationGate

职责：

- 判断是否可生成 `ConsolidationCandidate`
- 管理 candidate 提交、冷却、去重与过期
- 接收 `GovernanceFeedback` 并回写 cognition 状态

### 7.6 CognitiveMetabolismController

职责：

- 执行长期认知代谢
- 做 `metabolize`、压缩、归档、遗忘、复查
- 控制热点过热、冷数据堆积和隔离对象僵死

## 8. Recall Mutation 设计

`RecallMutationEngine` 的输入不是一次 search 结果，而是完整 recall 操作结果：

- `query`
- `recall_plan`
- `selected_results`
- `cited_results`
- `rejected_results`
- `final_answer_used_memories`
- `user_feedback`
- `tool_outcome`
- `conflict_signals`

### 8.1 变更类型

支持以下 5 类 mutation：

- `reinforce`
- `penalize`
- `guard`
- `quarantine`
- `contest`

### 8.2 强化抑制原则

为避免过度正反馈，采用以下约束：

- 强化收益随当前热度递减
- 新对象可获得有限 `novelty boost`
- 仅被召回但未被最终使用，不计为强正强化
- 长期未使用对象通过认知代谢逐步降温
- 对高频入选但长期不被使用的对象执行轻量抑制

### 8.3 输出结构

`RecallMutationEngine` 返回结构化结果：

- `state_updates`
- `generated_candidates`
- `quarantine_events`
- `contestation_events`
- `explanations`

该模块不直接写库。

## 9. Consolidation Gate 设计

### 9.1 输入

接受两类输入：

- `RecallMutationResult`
- `MetabolismReviewResult`

### 9.2 判定信号

最少考虑以下信号：

- `stability_score`
- `activation_score`
- `retrieval_success_count`
- `negative_feedback_count`
- `conflict_signals`
- `evidence_residual_score`
- `novelty_score`
- 近期重复提交情况

### 9.3 ConsolidationCandidate 结构

- `candidate_id`
- `source_owner_type`
- `source_owner_id`
- `tenant_id`
- `user_id`
- `project_id`
- `candidate_kind`
- `statement`
- `abstract`
- `overview`
- `supporting_memory_ids`
- `supporting_trace_ids`
- `confidence_estimate`
- `stability_score`
- `risk_score`
- `conflict_summary`
- `submission_reason`
- `created_at`
- `expires_at`
- `dedupe_fingerprint`

### 9.4 状态机

状态迁移：

- `none -> candidate`
- `candidate -> submitted`
- `submitted -> accepted`
- `submitted -> rejected`
- `candidate|submitted -> expired`

### 9.5 治理回流

`GovernanceFeedback` 回流后执行以下映射：

- `accepted`：提升稳定性，必要时进入 `guarded`
- `rejected`：降低巩固优先级，保留对象本体
- `contested`：进入 `exposure=contested`
- `deprecated`：撤销旧保护与旧加权

## 10. 认知代谢设计

本阶段统一采用术语：

- 上位概念：`Metabolism`
- 具体入口：`metabolize()`

### 10.1 代谢动作

`CognitiveMetabolismController` 管理以下动作：

- `metabolize`
- `compress`
- `archive`
- `forget`
- `review`

### 10.2 判定信号

- `activation_score`
- `last_accessed_at`
- `access_count`
- `stability_score`
- `risk_score`
- `consolidation_state`
- `exposure_state`
- `compression_ratio`
- `recent_mutation_density`

### 10.3 基本迁移

- `active -> compressed`
- `compressed -> archived`
- `archived -> forgotten`

隔离与争议对象还需支持周期复查：

- `quarantined -> guarded/open`
- `contested -> open/quarantined`

### 10.4 过热保护

必须显式支持：

- `activation ceiling`
- `diminishing returns`
- `recency rebalance`
- `dominance penalty`

## 11. 完整数据流

### 11.1 Ingest

- 内容写入原 collection
- `CognitiveStateManager.initialize(owner)`
- 创建初始 `cognitive_state`

### 11.2 Recall

- `RecallPlanner` 生成 plan
- `HierarchicalRetriever + ConeScorer` 执行召回
- `ConeScorer` 当前按实体共现索引执行扩展与路径打分，不假定显式关系图
- `RecallMutationEngine.apply(recall_outcome)`
- `CognitiveStateManager.persist(...)`

### 11.3 Consolidation

- `ConsolidationGate.evaluate(...)`
- 生成并写入 `ConsolidationCandidate`
- 派发给 `Knowledge Governance`

### 11.4 Governance Feedback

- 接收 `GovernanceFeedback`
- `ConsolidationGate.apply_feedback(...)`
- 回写 `cognitive_state`

### 11.5 Metabolism

- `CognitiveMetabolismController.tick()`
- 执行周期性代谢与复查

## 12. 与现有实现的接缝

### 12.1 保留现有内容面

以下内容 collection 保持不变：

- `context`
- `trace`
- `knowledge`

### 12.2 渐进接入点

第一阶段通过以下接缝接入：

- `MemoryOrchestrator` 写入路径：初始化 `cognitive_state`
- `MemoryOrchestrator` 搜索路径：在 recall 结束后执行 mutation
- `feedback()/protect()/reward_score/active_count/accessed_at`：短期保留兼容

### 12.3 现有 metadata 的地位

现有字段继续存在，但职责下调为：

- 检索辅助信号
- 兼容层字段

认知真相源以 `cognitive_state` 为准。

### 12.4 alpha 模块接缝

现有：

- `Archivist`
- `Sandbox`
- `KnowledgeStore`

短期不纳入 `Autophagy` 内核，只通过：

- `ConsolidationCandidate`
- `GovernanceFeedback`

与认知层对接。

## 13. Python 与 Rust/PyO3 边界

### 13.1 Python 控制面

保留 Python 负责：

- `AutophagyKernel` 编排
- 状态机装配
- store 访问
- knowledge/HTTP/MCP 集成

### 13.2 可替换热路径

以下接口预留 Rust/PyO3 实现位：

- `MutationScorer`
- `MetabolismEvaluator`
- `CandidateEligibilityScorer`
- `ConePathScorer`

### 13.3 实现形态

每个热路径接口允许双实现：

- `PythonBaseline`
- `RustAccelerated`

上层编排只依赖接口，不依赖具体语言。

## 14. 可观测性

每个关键状态变化必须产出可解释事件：

- `state_initialized`
- `recall_mutated`
- `candidate_created`
- `governance_feedback_applied`
- `metabolized`
- `quarantined`
- `contested`
- `archived`
- `forgotten`

每条事件最少包含：

- `owner_id`
- `before_state`
- `after_state`
- `reason`
- `source`
- `timestamp`
- `version`

## 15. 性能测试面

后续全量性能测试至少覆盖：

### 15.1 写入链路

- ingest 吞吐
- `cognitive_state` 初始化写放大

### 15.2 Recall 链路

- recall + mutation 总延迟
- cone 扩展后的 `P50 / P95 / P99`
- 命中规模上升时的退化曲线

### 15.3 Metabolism 链路

- full sweep 总耗时
- 批量扫描成本
- 状态转移吞吐

### 15.4 Candidate / Feedback 链路

- candidate 生成速率
- dedupe 开销
- feedback 回写延迟

### 15.5 数据规模

至少测以下数量级：

- `10^3`
- `10^4`
- `10^5`

并覆盖：

- 单租户
- 多租户
- `PythonBaseline`
- `RustAccelerated`（若已实现）

## 16. 设计结果

本阶段完成后，OpenCortex 将具备以下结构性变化：

- cognition 真相从 payload metadata 分离为独立 `cognitive_state`
- recall 后 mutation 收口到 `Autophagy`
- cognition 到 knowledge 的边界由 `ConsolidationCandidate` 固定
- 长期认知调节统一收口到 `Metabolism`
- 性能优化有清晰的 Rust/PyO3 热路径边界

这使 `Autophagy Kernel Phase 2` 可以作为独立子项目进入实施计划，而不与 `Knowledge Governance Layer` 或 `SkillEngine` 再次耦合。
