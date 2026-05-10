# Autophagy 功能细节文档

## 定位

Autophagy 是长期记忆和 trace 的生命周期治理层。它不负责原始写入，也不负责
具体向量检索算法；它负责在记忆被召回、使用、冲突、冷却、压缩、归档、遗忘
时维护认知状态。

在新链路中，Autophagy 应作为旁路状态机存在：

- 写入完成后初始化 cognitive state。
- 召回完成后根据 outcome 做 mutation。
- 周期性执行 metabolism。
- 将稳定的候选输出给 knowledge/skill 等下游。

它不应该吸收 Skill Engine，也不应该直接管理 Qdrant primary record 的写入。

## 核心对象

### OwnerType

Autophagy 管理的对象类型：

- `memory`
- `trace`

后续如果要管理 resource，应明确是否归类为 memory，还是新增 owner type。

### CognitiveState

每个 owner 有一条 cognitive state。

核心字段：

- 身份：
  - `state_id`
  - `owner_type`
  - `owner_id`
  - `tenant_id`
  - `user_id`
  - `project_id`
- 生命周期：
  - `lifecycle_state`
  - `exposure_state`
  - `consolidation_state`
- 分数：
  - `activation_score`
  - `stability_score`
  - `risk_score`
  - `novelty_score`
  - `evidence_residual_score`
- 召回反馈：
  - `access_count`
  - `retrieval_success_count`
  - `retrieval_failure_count`
  - `last_accessed_at`
  - `last_reinforced_at`
  - `last_penalized_at`
- mutation：
  - `last_mutation_at`
  - `last_mutation_reason`
  - `last_mutation_source`
  - `version`
- 扩展：
  - `metadata`

### LifecycleState

生命周期状态：

- `active`
- `compressed`
- `archived`
- `forgotten`

语义：

- `active`：正常参与召回。
- `compressed`：低活跃但仍可保留摘要或压缩表达。
- `archived`：长期冷却，默认不进入普通召回。
- `forgotten`：逻辑遗忘，不再作为有效认知对象使用。

### ExposureState

暴露状态：

- `open`
- `guarded`
- `quarantined`
- `contested`

语义：

- `open`：正常暴露。
- `guarded`：需要谨慎使用，多用于已治理但敏感/重要的知识。
- `quarantined`：隔离，默认不参与。
- `contested`：存在冲突，召回时需要标注或降权。

### ConsolidationState

巩固状态：

- `none`
- `candidate`
- `submitted`
- `accepted`
- `rejected`
- `expired`

语义：

- `candidate` 表示该 state 有资格形成长期知识候选。
- `submitted` 表示已提交给治理层。
- `accepted/rejected` 由治理反馈回写。

## 核心组件

### AutophagyKernel

编排门面。

职责：

- 初始化 owner state。
- 应用召回结果。
- 调用 mutation engine。
- 持久化 mutation batch。
- 调用 consolidation gate。
- 持久化 consolidation candidates。
- 执行 metabolism。
- 执行分页 sweep。

不负责：

- 生成 primary record。
- 直接做向量检索。
- 直接生成 skill。
- 直接修改用户原始 content。

### CognitiveStateStore

状态持久化层。

职责：

- 创建 cognitive state collection。
- save/get state。
- 按 owner 批量获取。
- scroll states。
- persist mutation batch。
- 版本校验。

约束：

- mutation 必须带 `expected_version`。
- 写入时 version 递增。
- mutation batch 是审计边界。

### RecallMutationEngine

召回后状态变更引擎。

输入：

- query。
- cognitive states。
- recall outcome。

`recall_outcome` 可包含：

- `final_answer_used_memories`
- `selected_results`
- `cited_results`
- `rejected_results`
- `conflict_events`

行为：

- used memory：
  - 增加 activation。
  - 更新 `last_reinforced_at`。
  - mutation reason = `reinforce`。
- recalled but not used 且过热：
  - 降低 activation。
  - 更新 `last_penalized_at`。
  - mutation reason = `penalize`。
- conflict：
  - exposure -> `contested`。
  - mutation reason = `contest`。
- touched：
  - `access_count + 1`。
  - 更新 `last_accessed_at`。

输出：

- `state_updates`
- `contestation_events`
- `explanations`

### CognitiveMetabolismController

长期代谢控制器。

输入：

- states。
- dominance window。

规则：

- archived 且极冷低价值：
  - -> `forgotten`
- compressed 且冷低价值：
  - -> `archived`
- active 且冷低价值：
  - -> `compressed`
- hot 且在 dominance window 里反复占优：
  - 降低 activation，避免赢家通吃。

value heuristic：

```text
value = stability_score - risk_score
```

输出：

- `state_updates`
- `review_events`

### ConsolidationGate

巩固候选生成器。

输入：

- cognitive states。

候选条件：

- `consolidation_state == candidate`
- metadata 中有 `statement`

输出：

- `ConsolidationCandidate[]`
- state updates：
  - consolidation_state -> `submitted`
  - metadata 写入 candidate id/fingerprint

去重：

- 基于 tenant/user/project/kind/statement/abstract/overview/supporting ids 生成 fingerprint。
- cooldown 窗口内重复候选不再提交。

### CandidateStore

巩固候选持久化。

职责：

- 初始化 candidate collection。
- 构建 fingerprint。
- cooldown 去重。
- 批量保存 candidates。
- 删除 candidates。

## 主要流程

### 1. 写入后初始化

当新链路写入 primary record 后：

1. worker 触发 Autophagy 初始化。
2. owner_type = `memory`。
3. owner_id = primary record URI 或稳定 record id。
4. 写入默认 CognitiveState。

初始化不应阻塞主写入。

### 2. 召回后 mutation

当 `/memory/search` 完成后：

1. 从最终结果提取 owner ids。
2. 构造 recall outcome。
3. 调用 `AutophagyKernel.apply_recall_outcome()`。
4. mutation engine 生成 updates。
5. state store 持久化 recall mutation batch。
6. 刷新 states。
7. consolidation gate 判断是否生成候选。
8. candidate store 保存 candidates。
9. 如果 consolidation state 有变化，再持久化 consolidation batch。

### 3. 周期性 metabolism

后台 sweep：

1. 分页读取 cognitive states。
2. 调用 metabolism controller。
3. 如果有 updates，写 mutation batch。
4. 返回 next cursor。
5. 失败 owner 记录到 result。

### 4. 治理反馈回写

当 knowledge governance 返回反馈：

1. 根据 candidate_id 找到 owner state。
2. `accepted`：
   - consolidation -> `accepted`
   - exposure -> `guarded`
3. `rejected`：
   - 如果有新证据，consolidation -> `none`
   - 否则 consolidation -> `rejected`
4. `contested`：
   - consolidation -> `rejected`
   - exposure -> `contested`
5. `deprecated`：
   - exposure -> `open`

## 与新链路关系

Autophagy 应接在这些位置：

- `memory_stored` 后初始化 state。
- `session_merged` / `session_ended` 后初始化 state。
- `memory_search` 返回后提交 recall outcome。
- worker 定时执行 metabolism sweep。

不建议接在：

- `PrimaryRecordWriter` 内部。
- `RetrievalExecutor` 内部。
- Qdrant vector store 内部。

## 迁移到 opencortex 的建议结构

建议新增：

- `src/opencortex/autophagy/schemas.py`
- `src/opencortex/autophagy/state_store.py`
- `src/opencortex/autophagy/mutation.py`
- `src/opencortex/autophagy/metabolism.py`
- `src/opencortex/autophagy/consolidation.py`
- `src/opencortex/autophagy/kernel.py`
- `src/opencortex/autophagy/actions.py`

数据模型建议全部使用 Pydantic。

## 验收标准

- 写入后能创建 CognitiveState。
- 召回后能 reinforce used records。
- 未使用但高 activation records 能被 penalize。
- conflict 能将 exposure 标记为 contested。
- metabolism 能 active -> compressed -> archived -> forgotten。
- consolidation candidate 能去重并持久化。
- 所有 mutation 有 batch 记录。
- 不依赖旧 `CortexMemory`、旧 `ContextManager`、旧 `services`。
