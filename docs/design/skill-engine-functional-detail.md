# Skill Engine 功能细节文档

## 定位

Skill Engine 用来管理可复用的操作能力。它不是普通 memory，也不是 resource。
它是一类独立资产，拥有自己的状态、谱系、质量、可见性、审批和演化流程。

新链路清理旧代码时，Skill Engine 不应该被并入 Autophagy，也不应该由 memory
store 直接管理。它应该作为独立下游模块，消费已治理的记忆、知识或人工输入，
生成可复用 skill。

## 核心对象

### SkillRecord

一个 skill 的主记录。

字段：

- `skill_id`
- `name`
- `description`
- `content`
- `category`
- `status`
- `visibility`
- `lineage`
- `tags`
- `tenant_id`
- `user_id`
- `project_id`
- `uri`
- 使用指标：
  - `total_selections`
  - `total_applied`
  - `total_completions`
  - `total_fallbacks`
- 语义字段：
  - `abstract`
  - `overview`
- 时间：
  - `created_at`
  - `updated_at`
- 去重：
  - `source_fingerprint`
- 质量：
  - `rating`
  - `tdd_passed`
  - `quality_score`
  - `reward_score`

### SkillStatus

- `candidate`
- `active`
- `deprecated`

流程：

```text
candidate -> active
candidate -> deprecated
active -> deprecated
```

不建议自动从 candidate 进入 active，除非后续明确引入自动审批策略。

### SkillVisibility

- `private`
- `shared`

语义：

- private：只有 owner 可见。
- shared：同 tenant 下可见。

禁止跨 tenant 共享。

### SkillOrigin

- `imported`
- `captured`
- `derived`
- `fixed`

语义：

- imported：外部导入。
- captured：从 memory/knowledge 中抽取。
- derived：基于已有 skill 派生新技能。
- fixed：修复已有 skill，生成新 candidate，不原地覆盖。

### SkillLineage

记录 skill 来源和演化关系。

字段：

- `origin`
- `generation`
- `parent_skill_ids`
- `source_memory_ids`
- `change_summary`
- `content_diff`
- `content_snapshot`
- `created_by`
- `created_at`

## 核心组件

### SkillManager

顶层编排。

职责：

- search。
- list。
- get。
- approve。
- reject。
- deprecate。
- promote。
- extract。
- fix。
- derive。
- 调用质量门。
- 调用 sandbox TDD。
- 保存候选。

权限规则：

- 读：
  - tenant 必须一致。
  - shared 同 tenant 可读。
  - private 只有 owner 可读。
- 写：
  - approve/reject/deprecate/promote/fix/derive 需要 owner。

### SkillStore

skill 持久化。

职责：

- save record。
- load record。
- load active。
- load by status。
- search。
- update status。
- update visibility。
- record selection/application。
- update reward。
- find by fingerprint。

建议新链路实现：

- 使用独立 Qdrant collection。
- 不与 memory primary collection 混用。

### SkillAnalyzer

从来源数据中抽取 skill suggestions。

当前旧实现来源：

- memory clusters。

流程：

1. scan memories。
2. cluster。
3. 对 cluster 生成 fingerprint。
4. fingerprint 已存在则跳过。
5. 加载已有 active skills。
6. 构造 LLM prompt。
7. LLM 返回候选建议。
8. 输出 `EvolutionSuggestion[]`。

后续建议：

- 来源改为新链路 retrieval/knowledge 输出。
- 不再直接依赖旧 Qdrant adapter。

### SkillEvolver

把 suggestion 转换成 SkillRecord。

支持三类演化：

- captured：
  - 从记忆/知识中捕获。
  - skill_id 基于 source fingerprint，保证幂等。
- derived：
  - 基于父 skill 派生。
  - 使用新 UUID。
  - generation = parent.generation + 1。
- fixed：
  - 修复父 skill。
  - 创建新 candidate。
  - 不原地修改父 skill。

LLM 演化循环：

- 最多 5 轮。
- LLM 需要输出终止标记：
  - `<EVOLUTION_COMPLETE>`
  - `<EVOLUTION_FAILED>`
- complete 后去掉标记，保存正文。
- failed 或超过轮次则放弃。

### QualityGate

质量门。

两层：

1. 规则校验。
2. 可选 LLM 语义校验。

规则校验：

- name 必须 lowercase-hyphenated。
- name 长度 <= 50。
- content 长度 > 50。
- content 必须包含步骤。
- description 非空。
- category 合法。
- content 不应过长。
- 不应有明显空 section。

计分：

- ERROR 每个扣 20。
- WARNING 每个扣 5。
- 分数低于 60 不保存。

LLM 语义校验：

- actionable。
- consistent。
- specific。
- duplicate。

### SandboxTDD

可选验证器，默认关闭。

流程：

1. 基于 skill 生成 2-3 个压力场景。
2. 对每个场景跑 baseline。
3. 对每个场景跑 with-skill。
4. 比较选择是否变好。
5. 通过条件：
   - improved >= 50%
   - worse == 0

限制：

- 这是 LLM 模拟测试，不是真实执行测试。
- 成本较高。
- 适合 candidate 质量过滤，不适合在线请求。

### SkillEventStore

记录使用事件。

事件字段：

- `event_id`
- `session_id`
- `turn_id`
- `skill_id`
- `skill_uri`
- `tenant_id`
- `user_id`
- `event_type`
- `outcome`
- `timestamp`
- `evaluated`

事件类型：

- `selected`
- `cited`

### SkillEvaluator

将 skill events 与 session outcome 关联。

流程：

1. 读取 session 下未评估事件。
2. 读取 trace/session outcome。
3. 按 skill_id 分组。
4. selected -> selection count +1。
5. cited -> application count +1。
6. session success -> completion count +1。
7. reward：
   - success +0.1
   - failure -0.05
8. 标记 evaluated。

具备 crash recovery：

- startup sweeper 扫描 unevaluated events。
- 按 session 分组重放 evaluate。

### Patch Engine

用于 skill 内容更新。

支持三类格式：

- full replacement。
- patch block。
- search/replace diff。

匹配策略：

- exact。
- rstrip。
- strip。
- unicode normalize。

注意：

- Patch Engine 是 skill 内容编辑工具，不应该直接操作代码仓库文件。
- 如果要支持真实自我升级代码修改，应放到 Self Upgrade 模块，不应混进 Skill Engine。

## API

旧实现接口：

- `GET /api/v1/skills`
- `GET /api/v1/skills/search`
- `POST /api/v1/skills/extract`
- `GET /api/v1/skills/{skill_id}`
- `POST /api/v1/skills/{skill_id}/approve`
- `POST /api/v1/skills/{skill_id}/reject`
- `POST /api/v1/skills/{skill_id}/deprecate`
- `POST /api/v1/skills/{skill_id}/promote`
- `POST /api/v1/skills/{skill_id}/fix`
- `POST /api/v1/skills/{skill_id}/derive`

迁移到新链路时建议保留 API 语义，但内部使用：

- `opencortex.core.identity`
- 新 vector store。
- 新 LLM client。
- 新 CFS/CortexStorage。

## 与 Autophagy 的关系

Autophagy 不生成 skill。

正确关系：

```text
memory/resource/session -> Autophagy cognitive state
Autophagy -> consolidation candidate
Knowledge governance -> governed knowledge
Skill Engine -> consume governed knowledge -> skill candidate
```

Skill Engine 可以消费：

- 已治理 knowledge。
- 高质量 memory cluster。
- 人工输入。

不应直接消费：

- 未治理的 contested memory。
- quarantined memory。
- raw trace。

## 后续重实现任务

1. 新建 `src/opencortex/skill_engine/`。
2. 所有 dataclass 改 Pydantic。
3. 独立 Qdrant collection。
4. 独立 routes。
5. 接入新 identity。
6. 接入新 LLM client。
7. 接入新 vector embedder。
8. 重新实现 source adapter，来源使用新链路 records。
9. 保留 quality gate 和 sandbox TDD。
10. 事件评估改用新 session outcome。

## 验收标准

- 可以创建 candidate skill。
- 可以 approve/reject/deprecate。
- private/shared 权限正确。
- search 只返回可见 active skills。
- captured skill 具备 fingerprint 幂等。
- fixed skill 不覆盖父 skill。
- QualityGate 拦截低质量候选。
- SkillEvent 可记录并被 evaluator 消费。
- 不依赖旧 `CortexMemory` 和旧 request context。
