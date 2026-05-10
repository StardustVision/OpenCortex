# 自我升级功能细节文档

## 定位

自我升级是系统把运行中积累的经验转化为可验证改进的能力。它不是单一模块，
而是一条跨 memory、Autophagy、Knowledge、Skill Engine、评估与补丁应用的闭环。

在 OpenCortex 中，自我升级应拆成两类：

1. **能力升级**
   - 生成或改进 skill。
   - 改进提示词。
   - 改进操作流程。
2. **系统升级**
   - 提出代码变更建议。
   - 生成 patch。
   - 运行测试。
   - 进入人工审核。

当前旧代码里已经有 Skill Engine 的 skill evolution、QualityGate、SandboxTDD、
Patch Engine，但还没有一个完整、受控的自我升级内核。后续应在新链路上重新
设计，而不是让 Skill Engine 直接修改系统代码。

## 输入来源

自我升级可以消费这些信号：

- memory 写入结果。
- session final。
- retrieval miss。
- user feedback。
- forget/update/mutation events。
- Insights 报告中的 friction。
- SkillEvent 使用结果。
- Autophagy consolidation candidates。
- failed worker events。
- search debug traces。

不应直接消费：

- 未脱敏的敏感原文。
- quarantined cognitive state。
- contested state 且无治理结论的数据。

## 核心对象

### UpgradeCandidate

表示一个可评审的升级候选。

建议字段：

- `candidate_id`
- `tenant_id`
- `user_id`
- `project_id`
- `candidate_type`
  - `skill`
  - `prompt`
  - `retrieval_policy`
  - `writer_policy`
  - `code_patch`
- `title`
- `problem_statement`
- `evidence`
- `proposed_change`
- `expected_impact`
- `risk_level`
- `source_refs`
- `status`
  - `candidate`
  - `validated`
  - `rejected`
  - `approved`
  - `applied`
  - `rolled_back`
- `created_at`
- `updated_at`

### UpgradeEvidence

升级证据。

来源：

- repeated failures。
- high-friction insights。
- retrieval miss cluster。
- repeated manual fix。
- skill low reward。
- worker failed queue。

字段：

- `source_type`
- `source_uri`
- `summary`
- `score`
- `metadata`

### UpgradePlan

升级执行计划。

字段：

- `candidate_id`
- `steps`
- `affected_modules`
- `validation_plan`
- `rollback_plan`
- `requires_human_review`

### UpgradeResult

执行结果。

字段：

- `candidate_id`
- `status`
- `applied_artifacts`
- `test_results`
- `quality_score`
- `errors`
- `rollback_available`

## 能力升级流程

能力升级不改代码，只生成或修改 skill/prompt/policy。

流程：

1. 收集信号。
2. 聚合成 UpgradeEvidence。
3. LLM 生成 UpgradeCandidate。
4. 分类：
   - skill candidate。
   - prompt candidate。
   - retrieval policy candidate。
5. QualityGate 检查。
6. SandboxTDD 或离线样例验证。
7. 进入 candidate 状态。
8. 人工 approve。
9. 激活。

适合自动化的场景：

- 生成新 skill。
- 修复已有 skill。
- 改进内部操作手册。
- 改进 prompt 草案，但不直接上线。

不应自动化的场景：

- 删除能力。
- 修改安全策略。
- 修改生产检索权重。

## 系统升级流程

系统升级会产生代码或配置变更，因此必须更严格。

流程：

1. 收集 evidence。
2. 生成 UpgradeCandidate。
3. 生成 UpgradePlan。
4. 生成 patch。
5. 应用到隔离工作区。
6. 运行测试。
7. 生成 diff summary。
8. 人工 review。
9. approve 后合并。
10. 记录 UpgradeResult。

约束：

- 不允许运行时直接改当前生产代码。
- 不允许绕过测试。
- 不允许自动 push。
- 不允许处理 secrets。
- 不允许把用户隐私写进 patch。

## Patch Engine 边界

现有 `skill_engine/patch.py` 支持：

- FULL。
- PATCH。
- DIFF。
- fuzzy anchor matching。

在自我升级中，它只能作为“文本补丁解析器”复用。

不能直接复用为最终系统代码修改器，原因：

- 没有仓库级安全边界。
- 没有测试执行。
- 没有 git 状态保护。
- 没有 review gate。
- 没有 rollback。

建议新增独立模块：

- `src/opencortex/self_upgrade/schemas.py`
- `src/opencortex/self_upgrade/evidence.py`
- `src/opencortex/self_upgrade/planner.py`
- `src/opencortex/self_upgrade/patcher.py`
- `src/opencortex/self_upgrade/validator.py`
- `src/opencortex/self_upgrade/routes.py`

## 与 Skill Engine 的关系

Skill Engine 是自我升级的一个执行目标，但不是自我升级本身。

关系：

```text
signals -> upgrade candidate -> skill evolution -> skill candidate -> approve -> active
```

Skill Engine 负责：

- skill 抽取。
- skill 演化。
- skill 质量门。
- skill 使用反馈。

Self Upgrade 负责：

- 识别系统改进机会。
- 分类升级类型。
- 决定是否走 skill、prompt、policy、code patch。
- 组织验证和审批。

## 与 Insights 的关系

Insights 提供高层趋势信号。

可以转化为 upgrade evidence：

- 高频 friction。
- 重复失败工具。
- 某类任务完成率低。
- 用户频繁中断。
- 某项目反复出现同类修复。

Insights 不直接生成 patch。

## 与 Autophagy 的关系

Autophagy 提供 cognitive state 和 consolidation candidates。

可以转化为 upgrade evidence：

- 高稳定高价值 memory。
- repeated reinforced pattern。
- contested pattern。
- archived 但被反复召回的对象。

Autophagy 不直接生成 skill 或 code patch。

## 与新写入/召回链路的关系

Self Upgrade 可以消费：

- worker failed queue。
- search debug trace。
- retrieval miss。
- reason tree 选择失败。
- update check result。

但不应在主写入或主召回路径同步执行。

建议全部异步：

- worker action。
- scheduled job。
- admin triggered job。

## API 建议

后续可新增：

- `POST /api/v1/self-upgrade/candidates/generate`
- `GET /api/v1/self-upgrade/candidates`
- `GET /api/v1/self-upgrade/candidates/{id}`
- `POST /api/v1/self-upgrade/candidates/{id}/validate`
- `POST /api/v1/self-upgrade/candidates/{id}/approve`
- `POST /api/v1/self-upgrade/candidates/{id}/reject`
- `POST /api/v1/self-upgrade/candidates/{id}/apply`

默认：

- 只有 admin 可调用 code_patch 类型 apply。
- 普通用户只能生成/审批自己的 skill/prompt 类候选。

## 安全要求

必须满足：

- 所有候选可审计。
- 所有 evidence 可追溯。
- 所有 code patch 必须人工审批。
- 所有 patch 必须在隔离环境验证。
- 所有失败必须记录。
- 所有 applied 结果必须可回滚或至少可禁用。

## 后续实现顺序

### P0：能力升级，不改代码

- 从 Insights/SkillEvent/failed queue 生成 skill upgrade candidates。
- 接 Skill Engine 的 `fix` / `derive`。
- 人工 approve。

### P1：Prompt/Policy 候选

- 生成 prompt/policy candidate。
- 只保存，不自动上线。
- 提供 diff 和验证样例。

### P2：Code Patch 候选

- 生成 patch。
- 应用到临时工作区。
- 运行测试。
- 生成报告。
- 人工 review。

### P3：受控 apply

- admin approve 后 apply。
- 记录 git diff。
- 支持 rollback/disable。

## 验收标准

- 可以从信号生成 UpgradeCandidate。
- candidate 有 evidence。
- skill 类型 candidate 能进入 Skill Engine。
- prompt/policy 类型 candidate 能保存并审阅。
- code patch 类型 candidate 不会自动改生产代码。
- 验证失败不会影响主链路。
- 所有操作有状态和审计记录。
