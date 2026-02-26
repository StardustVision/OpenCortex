# MemCortex 架构设计文档（阶段草案 v0.1）

## 0. 文档说明

- 文档目标：沉淀当前会话已确认的技术决策与细节设计草案，作为 Phase 1 的实施基线。
- 当前范围：优先完成本地持续记忆闭环 + 远程自学习候选技能闭环。
- 不在本阶段范围：候选 Skill 下发后执行策略与运行时效果治理（后续阶段讨论）。

---

## 1. Phase 1 核心目标

1. 在本地 AI 开发工作流中持续采集高价值记忆（优先支持 Claude Code、Cursor）。
2. 构建可靠的本地事件队列与微批处理机制，确保“不中断、不丢失、可恢复”。
3. 通过 MCP 同步到远程学习端，驱动 Agent Swarm 提炼候选 Skill。
4. 候选 Skill 以高频真实使用为核心，不追求每日固定产出。

---

## 2. 技术栈决策（已确认）

### 2.1 客户端与本地侧

- 接入客户端（Phase 1）：`Claude Code`、`Cursor`
- 集成方式：**MCP 触发**（本地运行 MCP server，通过 tools 触发采集与 flush）
- 本地向量存储：`LanceDB`（**仅存储向量 + 向量检索**，不做队列）
- 本地控制/队列存储：`SQLite`（spool 队列、元数据、状态机、dead letter）

### 2.2 远程侧

- 协议：`MCP`
- 编排框架：`Agno`（中心编排）
- 远程数据层（Phase 1.5）：`LanceDB + SQLite`
- 后续演进：满足阈值后评估迁移 `PostgreSQL`（控制面）

### 2.3 数据流主线

`capture(event) -> spool(sqlite) -> maybe_flush -> flush(vector to lancedb) -> sync(remote MCP)`

数据存储边界：
- **SQLite**：事件队列、状态机、元数据、dead letter（持久化控制流）
- **LanceDB**：已处理事件的向量 + 检索索引（用于后续学习/检索）

---

## 3. 本地架构详细设计

### 3.1 本地组件职责

1. Hook Adapter（按客户端捕获事件，通过 MCP tools 暴露）
2. Event Normalizer（统一事件模型）
3. Local Spool（SQLite 队列 + 状态机）
4. Flush Engine（微批处理，向量写入 LanceDB）
5. Sync Worker（低水位时通过 MCP 远程同步）
6. Vector Store（**LanceDB 专用**：存储事件向量 + 支持检索）

### 3.2 统一事件模型（MemoryEvent）

建议字段：

- `event_id`
- `source_tool` (`claude_code|cursor`)
- `session_id`
- `event_type` (`user_prompt|assistant_response|tool_use_end|session_end|error`)
- `content`（事件内容摘要）
- `embedding`（**向量**：内容已向量化的向量数据，LanceDB 存储用）
- `meta`（如项目路径、命令、退出码、文件引用）
- `domain_hint`（可选）
- `confidence`（可选）
- `created_at`

### 3.3 队列状态机

`new -> reserved -> processed -> synced`

失败分支：

`failed -> dead_letter`

### 3.4 flush 触发规则（OR 关系，已确认）

满足任一条件即触发 flush：

- 条数阈值：`reserved + processed 事件数 >= 20`
- 时间阈值：`队列中最老事件 age >= 180s`
- 空间阈值：`spool_size >= 100MB`

### 3.5 重试规则（已确认）

- 第 1 次失败：10 秒后重试
- 第 2 次失败：30 秒后重试
- 第 3 次失败：120 秒后重试
- 超过阈值：进入 `dead_letter`

### 3.6 并发与可靠性（已确认）

- 并发控制：文件锁（单 flush writer）
- 任务租约：`lease_timeout = 30s`
- backlog 定义：**reserved + processed 状态的事件总数**（待同步到远程的事件）
- 远程同步水位门：`backlog_low_watermark = 10`（触发 sync worker）

### 3.7 Hook 触发优先级（已确认）

1. **MCP trigger（外部强制）**：强制 flush（SessionEnd 等场景）
2. **PostToolUse**：常规 maybe_flush（普通采集后评估）
3. **PreSearch**：backlog >= 5 时先 flush（搜索前清理）
4. **PreToolUse**：仅采集，不 flush

> 注：MCP 触发机制通过本地 MCP server 的 tools 暴露，由客户端（如 Claude Code）主动调用。

### 3.8 保留与容量策略（已确认）

- 原始事件保留：7 天
- dead letter 处理：每日
- 每日清理后体积 > 70MB：compact
- 体积 >= 100MB：进入保护模式（仅保留高价值事件采集）
- 退出保护模式：体积 < 80MB 且 backlog < 10（reserved+processed 总数）

---

## 4. 远程 MCP 契约设计（MVP）

### 4.1 最小工具集（已确认）

1. `memory_ingest_batch`
2. `memory_sync_query`
3. `learning_run_daily`
4. `skill_candidate_list`
5. `skill_candidate_review`

### 4.2 `memory_ingest_batch` 关键约束

- 单批最大事件数：50
- 支持 `raw_content`（可选）
- 默认优先传 `content_summary`，敏感数据需脱敏并标记
- 幂等键：`event_id + payload_hash`

### 4.3 同步结果处理

- `accepted_ids`：本地标记 `synced`
- `duplicate_ids`：本地同样标记 `synced`
- `rejected`：按错误码重试/隔离/人工处理

### 4.4 审核策略

- `skill_candidate_review` 使用单人审核
- 必须记录：审核人、结论、备注、证据引用

---

## 5. 远程 Agent Swarm 设计（中心编排）

### 5.1 角色与职责

- `Orchestrator`：全流程状态推进与决策
- `Context Cleaner`：清洗、脱敏、质量标记
- `Memory Manager`：去重、合并、冲突标记、入库管理
- `Learning Leader`：模式归纳与频率统计
- `Skill Synthesizer`：候选 Skill 蓝图生成
- `Safety Auditor`：风险审查与拦截

### 5.2 流程原则

- Agent 输出结构化结果 + 置信度 + 证据引用
- 状态推进只由 Orchestrator 执行
- 每日运行可无产出（0 candidate 合法）

---

## 6. 候选 Skill 生成策略（高频驱动）

### 6.1 产出原则（已确认）

- 不要求每日产出 skill
- 仅高频、高复用、高成功率模式进入候选池

### 6.2 候选门槛（已确认）

- `support_count >= 8`
- `unique_sessions >= 3`
- `usage_count_7d >= 10`
- `success_rate >= 0.8`
- `heat_score >= 0.72`

### 6.3 模板聚类与观察池（已确认）

- 聚类方式：按任务模板聚类（非具体文件内容）
- 模板相似度阈值：`0.80`
- 高频但质量未达标：进入观察池（7 天）
- 升级条件：连续 7 天 `success_rate >= 0.85` 且 `usage_7d >= 10`

---

## 7. 远程控制面 SQLite 草案

### 7.1 建议核心表

- `tasks`
- `task_runs`
- `candidates`
- `reviews`
- `dead_letters`
- `audits`

### 7.2 表职责摘要

- `tasks`：任务状态机与租约重试
- `task_runs`：执行尝试明细与错误追踪
- `candidates`：候选 Skill 主体与热度指标
- `reviews`：人工审核记录
- `dead_letters`：异常隔离与处置轨迹
- `audits`：全链路操作审计日志

### 7.3 SQLite 运行建议

- `PRAGMA journal_mode=WAL;`
- `PRAGMA foreign_keys=ON;`
- `PRAGMA busy_timeout=5000;`
- `PRAGMA synchronous=NORMAL;`

---

## 8. Postgres 迁移触发条件（预埋）

满足任意条件时启动控制面迁移评估：

1. 日任务量 > 5 万
2. 并发 worker > 5 且锁等待明显
3. 审核/审计查询 P95 > 300ms 连续 7 天

---

## 9. 当前结论与下一步

### 9.1 当前结论

- Phase 1 架构路径清晰，具备直接实施条件。
- 设计重点已从“功能跑通”转向“可恢复、可追踪、可演进”。

### 9.2 下一步建议（实施前）

1. 先完成 `Claude Code + Cursor` 的 Hook 事件映射表
2. 确认 `MemoryEvent` 与 MCP 请求体 schema（v1 固定）
3. 补充控制面 6 表 DDL（草案转可执行）
4. 定义最小质量指标看板（错配率、纠错率、dead letter 率）


---

## 10. 一页实施执行清单（按周）

### 第 1 周（2026-02-12 ~ 2026-02-18）：本地采集骨架

- 完成 `Claude Code + Cursor` Hook 映射（中等兼容事件）
- 定义统一 `MemoryEvent` schema
- 实现 `capture`（仅入 SQLite spool）
- 验收标准：事件稳定入队，字段完整率 > 95%

### 第 2 周（2026-02-19 ~ 2026-02-25）：本地 flush 闭环

- 实现 `maybe_flush / flush`（20 条 / 180s / 100MB）
- 接入 LanceDB 本地写入与基础检索
- 完成重试与死信（10s -> 30s -> 120s -> dead letter）
- 验收标准：flush 可恢复、无重复写入、死信可追踪

### 第 3 周（2026-02-26 ~ 2026-03-04）：MCP 同步最小闭环

- 实现 `memory_ingest_batch`、`memory_sync_query`
- 完成本地状态回写（accepted/duplicate/rejected 分流）
- 接入低水位同步门（`backlog <= 10`）
- 验收标准：幂等正确、远程失败不影响本地主链路

### 第 4 周（2026-03-05 ~ 2026-03-11）：远程 Agno 编排 v1

- 上线中心 `Orchestrator`
- 接入 `Context Cleaner`、`Memory Manager`、`Learning Leader`
- 建立任务状态机（`tasks / task_runs`）
- 验收标准：任务可重试、可审计、可恢复

### 第 5 周（2026-03-12 ~ 2026-03-18）：候选 Skill 机制

- 上线 `Skill Synthesizer`、`Safety Auditor`
- 实现候选门槛：`support >= 8`、`usage_7d >= 10`、`success_rate >= 0.8`、`heat_score >= 0.72`
- 实现观察池（7 天）与人工审核流
- 验收标准：允许“0 产出日”，仅高价值候选入池

### 第 6 周（2026-03-19 ~ 2026-03-25）：稳定性与上线前检查

- 建立指标看板：错配率、纠错率、dead letter 率、跨域误用率
- 执行容量治理：7 天保留、70MB compact、100MB 保护模式
- 开展风险演练：远程故障、本地锁冲突、重复批次
- 验收标准：连续 7 天稳定运行，满足 Phase 1 发布条件

### Phase 1 发布门槛

- 本地链路可持续运行，事件不丢失
- 远程同步幂等与重试策略验证通过
- 候选 Skill 机制可解释、可审计、可人工把关
- 范围边界明确（不含 Skill 下发执行）
