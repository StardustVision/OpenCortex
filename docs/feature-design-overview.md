# OpenCortex 功能设计总览

> **版本**: 0.3.9 | **更新**: 2026-03-09
>
> 本文档基于当前代码实现，补充 OpenCortex 的功能设计、核心约束与关键算法设计。

---

## 目录

1. [系统概述](#系统概述)
2. [核心子系统](#核心子系统)
3. [API 设计](#api-设计)
4. [数据流](#数据流)
5. [核心数据模型与约束](#核心数据模型与约束)
6. [关键算法设计](#关键算法设计)
7. [配置设计](#配置设计)
8. [关键架构模式](#关键架构模式)
9. [Cortex Alpha 能力补齐](#cortex-evolution-能力补齐)
10. [测试覆盖](#测试覆盖)
11. [待开发功能](#待开发功能)

---

## 系统概述

OpenCortex 是一套**面向 AI 智能体的记忆与上下文管理系统**。其核心逻辑在于**不仅仅存储数据，而是通过反馈循环和认知演化（ACE）来优化记忆的检索质量与技能沉淀**。

**核心能力**：
- **持久化与自我改进**: 可持续召回、可反馈排序的长期记忆引擎。
- **三层记忆架构**: L0 摘要 (Payload 极速预览) / L1 概述 (结构化背景) / L2 全文 (按需加载)。
- **反馈驱动排序**: 强化学习奖励 (RL) + 时间指数衰减 + 访问热度。
- **自动化认知演化 (ACE)**: 零成本规则提取 + 技能双轨演化（观测 vs 活跃）。
- **多租户隔离**: tenant / user / project / scope 四级权限隔离。

**核心组件链路**：
1. **接入层**: MCP Server (Node.js) -> HTTP Server (FastAPI)
2. **调度层**: `MemoryOrchestrator` (核心调度中枢)
3. **检索层**: `IntentRouter` -> `HierarchicalRetriever` -> `RerankClient`
4. **存储层**: `CortexFS` (三层文件系统) + `VikingDB/Qdrant` (向量数据库)
5. **进化层 (ACE)**: `RuleExtractor` (零成本规则) + `Skillbook` (技能管理) + `SessionExtractor` (LLM 记忆提取)

---

## 核心子系统

### 1. MemoryOrchestrator
**文件**: `src/opencortex/orchestrator.py`
顶层协调器，负责把写入、搜索、反馈、会话、技能演化统一到一套 API。
- **写时去重 (Write-time Dedup)**: 在 `add()` 阶段执行语义去重（阈值 0.82），针对 `preferences` 等类别执行 Merge。
- **异步保鲜**: 搜索完成后异步更新 `active_count` 与 `accessed_at`，不阻塞响应。

### 2. CortexFS (三层存储架构)
**文件**: `src/opencortex/storage/cortex_fs.py`
三层文件系统抽象，负责 URI 与路径互转。
- **L0 (.abstract.md)**: 存入向量库，用于快速索引与 Top-K 预览。
- **L1 (.overview.md)**: 提供结构化背景，通过 `Overview-first` 算法保持与全文语义一致。
- **L2 (content.md)**: 完整内容，仅在需要 `detail_level=l2` 时从磁盘读取。

### 3. HierarchicalRetriever (层次化检索)
**文件**: `src/opencortex/retrieve/hierarchical_retriever.py`
利用 **Frontier Batching (波次检索)** 算法进行目录级定位，通过父子分数传播（Score Propagation）提高深层节点召回率。

### 4. IntentRouter (意图路由)
**文件**: `src/opencortex/retrieve/intent_router.py`
三层意图路由器：关键词匹配 -> LLM 语义分类 -> Trigger 扩展查询。

### 5. Skillbook & RuleExtractor (ACE 核心)
- **RuleExtractor**: 零 LLM 成本。利用正则和动作词库提取“错误-修复”、“用户偏好”和“工作流模式”。
- **Skillbook**: 管理技能生命周期（private -> candidate -> promoted -> deprecated）。

---

## API 设计 (略，详见 0.3.8 版本)

---

## 关键算法设计

### 1. Overview-first 写入算法
目标是先保证 L1 可读，再反向校正 L0。
- 若 `overview` 为空但有 `content`：通过 LLM 生成 `overview`。
- 最终校正 `abstract`，确保短摘要不会丢失 L1 中的关键检索词。

### 2. Frontier Batching 层次化检索
- **波次收敛**: 默认最多 8 轮 wave，若 Top-K 结果在连续 3 轮内稳定则提前停止。
- **分数传播**: `propagated_score = α * child_score + (1 - α) * parent_score`。
- **公平采样**: `per_parent_fair_select` 确保每个父目录下都有候选被选中，避免单一路径霸占结果集。

### 3. RL + 热度排序算法
`final_score = base_similarity + rl_weight * reward_score + hot_weight * hotness`
- **时间衰减**: `new_reward = old_reward * rate`。
- `rate`: 普通记忆 0.95，被保护记忆 0.99。

### 4. 技能双轨演化算法 (Dual-track Evolution)
- **观测轨 (Observation)**: 新提取的技能先进入观测期。
- **活跃轨 (Active)**: 只有置信度（Success Rate * Log(Usage)）达标后才正式转正。
- **回滚机制**: 若新技能表现不如被替代的旧技能，系统自动标记为 `deprecated` 并回滚。

### 5. Dense + Lexical 融合 (RRF)
利用 **Reciprocal Rank Fusion** 融合不同分值空间的检索结果：
`RRF(d) = (1 - b) / (k + rank_dense(d)) + b / (k + rank_lexical(d))`

---

## 关键架构模式

### 双写模式 (Dual-Write)
每条记录同时写入 **CortexFS** (持久化内容) 和 **Qdrant** (向量 + 排序 Payload)。搜索主路径零文件 I/O，L2 按需读取。

### 优雅降级 (Graceful Degradation)
- 无 LLM: 关闭会话提取与语义意图分析。
- 无 Rerank: 保留 Dense + Lexical 混合检索。
- 无 Embedder: 退化为关键词过滤与 Scroll 模式。

---

## Cortex Alpha 能力补齐

`Cortex Alpha` 的方向是正确的，但要从“概念架构”落到“可实施系统”，还必须补齐 5 类能力：状态机、数据契约、验证闭环、进化执行面、训练固化面。下面给出建议补充设计。

### 1. 需要新增的核心模块

#### 1.1 Observer / Trace Collector

目标：把当前 OpenCortex 的 session/case memory 扩展成**可训练、可回放、可审计**的轨迹资产。

**建议职责**：
- 在模型 API 与 Agent Runtime 之间插入 Proxy/Observer 层
- 记录每个 turn 的 prompt / thought / tool_call / tool_result / final_answer
- 标记 action token 与 environment token，支持后续训练 masking
- 生成标准化 Trace ID，与 session_id / case_uri / skill_uri 关联

**建议新增数据结构**：

| 字段 | 说明 |
|------|------|
| `trace_id` | 轨迹主键 |
| `session_id` | 所属会话 |
| `turn_id` | Turn 序号 |
| `prompt_text` | 输入 Prompt |
| `thought_text` | 中间推理 |
| `action_name` / `action_args` | 工具调用信息 |
| `observation_text` | 环境返回 |
| `final_text` | 最终输出 |
| `token_mask` | action/environment token 掩码 |
| `latency_ms` | 单轮时延 |
| `cost_meta` | token / 费用统计 |
| `outcome` | success / failure / timeout |
| `error_code` / `error_cause` | 失败归因 |

#### 1.2 Archivist Service

目标：把散乱 Trace 变成稳定的 belief / SOP / negative rule / candidate skill。

**建议职责**：
- L2 Trace -> L1 SOP 压缩
- 失败轨迹归因与否定规则提炼
- 冲突记忆消解
- 候选技能验证前置筛选

**建议工具接口**：
- `ADD_SKILL`
- `UPDATE_BELIEF`
- `MERGE_TRACE_CLUSTER`
- `FORGET_NOISE`
- `CREATE_NEGATIVE_RULE`

#### 1.3 Sandbox Evaluator

目标：把“提炼出的规则”从静态总结变成**经验证的可执行经验**。

**建议职责**：
- 在受控沙盒中回放 case / trace
- 跑反事实验证（counterfactual verification）
- 给 candidate skill / belief 打可解释分
- 生成 commit / rollback 建议

#### 1.4 Skill Arena

目标：把当前单技能双轨演化升级为**多变体竞争**。

**建议职责**：
- 接收一个 seed SOP 或高价值 skill
- 生成多个 prompt/code 变体
- 并行跑 benchmark / 回放集
- 选择 winner，更新到 registry

#### 1.5 Coach / Adapter Registry

目标：把验证通过的黄金轨迹与技能转成权重层资产，而不是一直停留在软件检索层。

**建议职责**：
- 收集 training-ready traces
- 发起 LoRA/QLoRA/AReaL-lite 训练任务
- 管理 adapter 版本、标签、挂载策略
- 支持 canary / rollback / retire

---

### 2. 需要补齐的状态机设计

当前方案里的 Agent 角色很多，但系统对象还没有状态机，这会导致实现阶段边界混乱。建议至少补齐以下 3 组状态机。

#### 2.1 Trace 生命周期

```text
captured
  -> labeled
  -> compressed
  -> verified
  -> training_ready
  -> archived
```

**状态说明**：
- `captured`: 原始轨迹已采集
- `labeled`: 已补 outcome / error / reward / token mask
- `compressed`: 已提炼出 SOP / root cause / belief
- `verified`: 已通过沙盒验证
- `training_ready`: 可作为 Coach 训练样本
- `archived`: 冻结，仅用于审计和回放

#### 2.2 Skill 生命周期

```text
draft
  -> candidate
  -> sandbox_verified
  -> observation
  -> active
  -> deprecated
  -> retired
```

**与现有 OpenCortex 的对齐关系**：
- 现有 `private_only/candidate/promoted/demoted/deprecated` 可继续保留作为共享视角状态
- 新增 `sandbox_verified` 与 `observation` 作为演化视角状态

#### 2.3 Adapter 生命周期

```text
queued
  -> training
  -> validated
  -> canary
  -> serving
  -> rollback
  -> retired
```

---

### 3. 需要补齐的数据契约

如果要走向 `Cortex Alpha`，单纯存 `abstract/overview/content` 不够，必须补一层面向演化的数据表。

#### 3.1 Trace Warehouse

建议新增集合或目录：
- `traces`
- `trace_segments`
- `trace_labels`

**最小字段**：
- `trace_id`
- `tenant_id`
- `project_id`
- `agent_id`
- `task_type`
- `environment`
- `reward`
- `outcome`
- `root_cause`
- `source_skill_uri`
- `source_case_uri`
- `created_at`

#### 3.2 Belief / SOP Store

当前 Skillbook 偏“技能句子”，还不够表达复杂 SOP。建议新增：
- `beliefs`
- `sops`
- `negative_rules`

**SOP 结构建议**：

| 字段 | 说明 |
|------|------|
| `sop_id` | 主键 |
| `objective` | 任务目标 |
| `preconditions` | 前置条件 |
| `action_steps` | 标准步骤 |
| `anti_patterns` | 禁止路径 |
| `success_criteria` | 成功标准 |
| `failure_signals` | 失败信号 |
| `source_trace_ids` | 来源轨迹 |
| `confidence` | 稳定度 |
| `last_verified_at` | 最近验证时间 |

#### 3.3 Evaluation Record

Skill / Belief / Adapter 都需要统一评测记录。

**建议新增字段**：
- `eval_id`
- `target_type` (`skill` / `belief` / `adapter`)
- `target_uri`
- `benchmark_set`
- `success_rate`
- `latency_score`
- `cost_score`
- `safety_score`
- `robustness_score`
- `fitness_score`
- `winner_against`
- `evaluated_at`

---

### 4. 需要补齐的验证与治理闭环

这是这份方案里最应该补的一段。没有验证闭环，系统会把噪声经验当知识；没有治理闭环，系统会把局部最优当长期最优。

#### 4.1 Skill / Belief Commit Gate

任一候选规则进入长时存储前，建议必须过 4 道门：

1. **静态质量门**
   - 长度、动作动词、条件结构、非敏感信息检查
2. **来源可信度门**
   - 至少来自 N 条成功 trace 或高质量 case
3. **沙盒验证门**
   - 在 replay/sandbox 上成功率达到阈值
4. **回归门**
   - 新规则不能显著降低历史 benchmark

#### 4.2 建议新增 Fitness 公式

不要只看成功率，建议改为多目标适应度：

```text
fitness =
  0.40 * success_rate +
  0.20 * robustness_score +
  0.15 * latency_score +
  0.10 * cost_score +
  0.10 * safety_score +
  0.05 * generalization_score
```

这样可以避免“高成功但高成本/高风险”的策略被错误晋升。

#### 4.3 冲突解决机制

当新 belief 与旧 belief 冲突时，不能只做覆盖。建议：

```text
if same objective and contradictory action:
    old -> observation
    new -> candidate_verified
    run head-to-head evaluation
    select winner
    loser -> deprecated or scoped_local
```

#### 4.4 审计与可追溯

建议每次 evolution / promotion / rollback 都生成 `decision_record`：
- 输入样本
- benchmark 集
- 评估指标
- 决策原因
- 审批人/系统
- 生效范围

---

### 5. 需要补齐的算法设计

#### 5.1 Root Cause Analysis 算法

当前文档提了根因分析，但没有写执行方式。建议最小实现为“两阶段归因”：

```text
1. 规则归因：
   - 参数错误
   - 工具不可用
   - 外部依赖失败
   - 规划错误
   - 记忆缺失
   - 安全拦截
2. LLM/Classifier 精修：
   - 输出 root_cause
   - 输出 confidence
   - 输出 recoverable_action
```

产物应直接进入 `negative_rules` 或 `repair_patterns`。

#### 5.2 Trace Compression 算法

建议不要只做摘要，而要做“簇级压缩”：

```text
Trace Cluster
  -> common objective
  -> repeated failed branches
  -> final successful branch
  -> delta between fail and success
  -> distilled SOP / anti-pattern
```

压缩目标：
- 减少 L2 冗余
- 让 L1 真正可用于下一轮决策
- 为训练提供高密度 supervision

#### 5.3 Active Dreaming Memory (ADM)

建议把 ADM 明确为离线 replay 框架，而不是模糊概念：

**输入**：
- 候选 belief / skill
- 历史 trace 集
- 合成场景模板

**输出**：
- `pass_rate`
- `failure_modes`
- `coverage_gain`
- `generalization_score`

只有达标对象才允许 commit 到 `active`。

#### 5.4 Evolutionary Loop

建议写成标准流程：

```text
seed skill
  -> mutation generation (N variants)
  -> benchmark rollout
  -> fitness scoring
  -> top-k selection
  -> canary deployment
  -> full promotion or rollback
```

**突变维度建议**：
- Prompt wording
- Step ordering
- Tool selection policy
- Retry policy
- Validation checkpoints

#### 5.5 Adapter Routing

既然你提出 Sharded LoRA，就要补“运行时如何挂载”：

```text
query/task
  -> L0/L1 intent + skill lookup
  -> adapter selector
  -> mount adapter set
  -> execute
  -> collect trace
```

建议选择依据：
- task_type
- environment
- tenant policy
- safety level
- required skill section

---

### 6. 与当前 OpenCortex 的衔接建议

为了避免一次性推翻现有架构，建议按 4 个阶段演进。

#### Phase A: Trace 化

在现有 `SessionManager` / `case memory` 基础上新增：
- Trace schema
- token/action/environment mask
- failure taxonomy
- trace warehouse

#### Phase B: Archivist 化

在现有 `RuleExtractor` / `mine_skills()` 基础上新增：
- root cause extraction
- negative rule store
- trace cluster compression
- sandbox verification queue

#### Phase C: Arena 化

在现有 `skill_feedback()` / `evolve_skill()` 基础上新增：
- 多变体生成
- benchmark set
- fitness scoring
- canary + rollback

#### Phase D: Coach 化

在 OpenCortex 之外或旁路新增：
- training job orchestration
- LoRA registry
- adapter selector
- serving rollout policy

---

### 7. 建议新增 API / Tool 能力

如果要让这套设计可操作，建议后续补以下接口：

| 接口 | 说明 |
|------|------|
| `trace_store` | 存原始/结构化 trace |
| `trace_label` | 回填 outcome/root_cause/reward |
| `trace_replay` | 回放指定 trace |
| `belief_add/update/list` | 管理 belief |
| `negative_rule_add/list` | 管理失败规约 |
| `skill_benchmark` | 对 skill 变体打分 |
| `skill_arena_run` | 跑多变体竞争 |
| `skill_commit` | 将 winner 提升为 active/promoted |
| `adapter_train` | 发起 LoRA/QLoRA 训练 |
| `adapter_registry_list` | 查询 adapter |
| `adapter_route_preview` | 查看当前任务会挂载哪些 adapter |

---

### 8. 建议新增测试面

除了现有 recall / skill / session 测试，`Cortex Alpha` 还必须新增：

- Trace 完整性测试
- Token mask 正确性测试
- Root cause 分类一致性测试
- Sandbox replay 可复现性测试
- Skill arena 选优稳定性测试
- Evolution rollback 测试
- Adapter registry 路由测试
- Benchmark regression 测试

---

### 9. 结论

`Cortex Alpha` 不应该只被理解为“更复杂的记忆系统”，而应被定义为一个四层闭环：

```text
Trace Capture
  -> Experience Distillation
  -> Skill / Belief Evolution
  -> Weight Consolidation
```

对 OpenCortex 来说，最现实的路径不是一次性做完，而是：
- 先把 `Memory` 升级成 `Trace-aware Memory`
- 再把 `ACE` 升级成 `Verified Evolution`
- 最后把 `Skillbook` 升级成 `Skill + Adapter Registry`

这样才能从“会记住”真正走到“会进化”。

---

## 待开发功能 (优先级)
1. **Event JSONL 镜像**: 增强可观测性。
2. **长会话压缩**: 降低 LLM 提取成本。
3. **图关系查询**: 利用 `.relations.json` 进行多跳检索。

---
*OpenCortex 内部设计文档 - 保持同步更新*
