# Insights 功能细节文档

## 定位

Insights 是面向用户和团队的会话洞察系统。它不是记忆写入链路的一部分，也不
应该参与召回排序。它消费已经持久化的 trace/session 数据，生成周期性报告，
帮助用户理解：

- 最近在做什么类型的工作。
- 哪些工具和语言使用最多。
- 会话是否成功。
- 有哪些摩擦点。
- 哪些工作方式有效。
- 后续可以改进什么。

旧实现主要位于 `src/opencortex/insights/`。后续如果清理旧链路，Insights 应
作为新链路旁路分析模块重新接入，而不是依赖旧 `CortexMemory` facade。

## 核心输入

Insights 的输入不是原始 memory，而是 trace/session 级数据。

主要输入字段：

- `session_id`
- `tenant_id`
- `user_id`
- `project_path`
- turn 列表
- user prompt
- assistant final text
- tool calls
- tool input params
- tool error
- token count
- turn status
- timestamp

如果新链路未来不再保留旧 `TraceStore`，需要提供等价的 session event source：

- 从 CFS session tree 读取。
- 或从 Qdrant session primary records 读取。
- 或从独立 trace collection 读取。

## 数据模型

### SessionMeta

`SessionMeta` 是单个 session 的确定性统计结果，不需要 LLM。

包含：

- session 身份：
  - `session_id`
  - `tenant_id`
  - `user_id`
  - `project_path`
- 时间：
  - `start_time`
  - `duration_minutes`
  - `message_hours`
  - `user_message_timestamps`
- 消息计数：
  - `user_message_count`
  - `assistant_message_count`
- 工具统计：
  - `tool_counts`
  - `tool_errors`
  - `tool_error_categories`
- 语言和代码变更：
  - `languages`
  - `files_modified`
  - `lines_added`
  - `lines_removed`
- git 行为：
  - `git_commits`
  - `git_pushes`
- token：
  - `input_tokens`
  - `output_tokens`
- 特殊能力使用：
  - `uses_agent`
  - `uses_mcp`
  - `uses_web_search`
  - `uses_web_fetch`
- 用户行为：
  - `first_prompt`
  - `user_interruptions`
  - `user_response_times`

### SessionFacet

`SessionFacet` 是单个 session 的 LLM 语义分析结果。

包含：

- `underlying_goal`
- `goal_categories`
- `outcome`
- `user_satisfaction_counts`
- `claude_helpfulness`
- `session_type`
- `friction_counts`
- `friction_detail`
- `primary_success`
- `brief_summary`
- `user_instructions_to_claude`

### AggregatedData

`AggregatedData` 是跨 session 聚合结果。

包含：

- 总 session 数。
- 总消息数。
- 总耗时。
- token 总量。
- 工具分布。
- 语言分布。
- 项目分布。
- 目标类型分布。
- outcome 分布。
- satisfaction/helpfulness 分布。
- friction 分布。
- success 分布。
- 多开/并行工作信号。
- 活跃天数。
- 每日消息量。

### InsightsReport

`InsightsReport` 是最终报告对象。

包含：

- `tenant_id`
- `user_id`
- `report_period`
- `generated_at`
- `total_sessions`
- `total_messages`
- `total_duration_hours`
- `session_facets`
- `project_areas`
- `what_works`
- `friction_analysis`
- `suggestions`
- `on_the_horizon`
- `at_a_glance`
- `interaction_style`
- 细节版本字段：
  - `what_works_detail`
  - `friction_detail`
  - `suggestions_detail`
  - `on_the_horizon_detail`
  - `fun_ending`
- `aggregated`
- `cache_hits`
- `llm_calls`

## 处理流程

### 1. 加载 sessions

输入：

- tenant
- user
- start_date
- end_date
- max sessions

行为：

1. 从 trace/session source 读取时间范围内的 sessions。
2. 排除空 session。
3. 对同一个 `session_id` 去重。
4. 过滤非实质 session：
   - user message 太少的 session。
   - 纯 warmup session。

### 2. 生成 SessionMeta

`SessionMetaExtractor` 不调用 LLM，只做确定性统计。

工具统计规则：

- 按 `tool_calls[].name` 计数。
- `Agent` 标记 `uses_agent=true`。
- `mcp__` 前缀标记 `uses_mcp=true`。
- `WebSearch` 标记 `uses_web_search=true`。
- `WebFetch` 标记 `uses_web_fetch=true`。

语言统计规则：

- 从 tool input 的 `file_path` 后缀推断语言。
- `.py` -> Python。
- `.ts/.tsx` -> TypeScript。
- `.js/.jsx` -> JavaScript。
- `.go` -> Go。
- `.rs` -> Rust。
- `.md` -> Markdown。
- `.json/.yaml/.yml` -> 数据格式。

错误分类规则：

- command failed。
- user rejected。
- edit failed。
- file changed。
- file too large。
- file not found。
- other。

代码变更规则：

- `Write`：按 content 行数记新增。
- `Edit`：比较 `old_string` 和 `new_string` 行数。
- `Edit/Write` 的 file_path 进入 modified file set。

### 3. Facet 抽取

对每个有效 session 构造 transcript：

- session header。
- user prompt。
- tool names。
- assistant final text。

短 transcript 直接送 LLM。

长 transcript：

1. 按 chunk size 拆分。
2. 每个 chunk 先摘要。
3. 摘要拼接后再抽 facet。

Facet 抽取输出必须是结构化 JSON，失败时该 session 不产生 facet。

### 4. 聚合

聚合输入：

- `SessionMeta[]`
- `SessionFacet{session_id -> facet}`
- 起止日期。

聚合输出：

- `AggregatedData`

聚合不应该调用 LLM。

### 5. 报告章节生成

基于 `AggregatedData` 并行生成多个 LLM section：

- interaction style。
- project areas。
- what works。
- friction analysis。
- suggestions。
- on the horizon。
- fun ending。

最后串行生成 `at_a_glance`，因为它依赖前面章节的综合结果。

### 6. 持久化

报告写入 CFS：

- JSON：
  - `opencortex://{tenant}/{user}/insights/reports/{date}/weekly.json`
- HTML：
  - `opencortex://{tenant}/{user}/insights/reports/{date}/weekly.html`
- latest metadata：
  - `opencortex://{tenant}/{user}/insights/meta/latest_report.json`

缓存写入 CFS：

- meta cache：
  - `opencortex://{tenant}/{user}/insights/cache/meta/{session_id}.json`
- facet cache：
  - `opencortex://{tenant}/{user}/insights/cache/facets/{session_id}.json`

## API

旧实现提供：

- `POST /api/v1/insights/generate`
- `GET /api/v1/insights/latest`
- `GET /api/v1/insights/history`
- `GET /api/v1/insights/report/{date}`

迁移到新链路时建议：

- 保留 API 名称。
- 内部依赖新 `storage.CortexStorage`。
- 身份从 `opencortex.core.identity` 获取。
- 不依赖旧 request context。
- 不依赖旧 `CortexMemory`。

## 与新链路关系

Insights 只消费新链路写出的 session/resource/memory 数据，不反向影响写入。

建议输入来源：

1. 优先：新链路 session final 和 merged records。
2. 可选：专门的 trace/event collection。
3. 可选：CFS session tree。

不建议：

- 从旧 `ContextManager` 读取会话状态。
- 从旧 `Observer` 作为唯一来源。

## 失败处理

必须可容忍：

- 单个 session facet LLM 失败。
- 单个 cache 文件损坏。
- 长 transcript 摘要失败。
- 报告部分章节 LLM 失败。

建议行为：

- 单 session 失败不影响整份报告。
- cache 损坏时删除并重新生成。
- section 失败时写空 section，并记录错误。
- 最终报告仍可生成，但带 `llm_calls` 和错误计数。

## 后续重实现任务

1. 新建 `src/opencortex/insights/`。
2. 将 dataclass 改为 Pydantic BaseModel。
3. 将 CFS 读写改为新 `CortexStorage`。
4. 将 trace source 抽象成新链路接口。
5. 接入新 identity middleware。
6. 保留报告 JSON/HTML 输出。
7. 增加针对 session records 的 e2e 测试。

## 验收标准

- 可以从新链路 session 数据生成 InsightsReport。
- 单个 LLM 失败不导致整个报告失败。
- 报告 JSON/HTML 都能写入 CFS。
- latest/history/report API 可用。
- 不 import 旧 `CortexMemory`、`ContextManager`、`opencortex.http.request_context`。
