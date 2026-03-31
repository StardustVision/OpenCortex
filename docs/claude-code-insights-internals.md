# Claude Code `/insights` 内建命令实现与提示词重建

## 1. 范围与来源

这份文档基于本机 Claude Code 二进制的静态字符串提取与运行时路径观察整理，不是官方源码。

- 内建命令载体：`/Users/hugo/.local/share/claude/versions/2.1.87`
- 整体字符串提取：`/tmp/claude-2.1.87.strings.txt`
- 局部提取片段目录：`/tmp/claude-insights-prompts/`

说明：

- `/insights` 是内建命令，不在当前 `claude-code` 开源仓库中。
- 当前可定位到的是二进制中的提示词模板、数据流和输出结构。
- 下面的“完整提示词”是按原片段做的忠实重建与结构化整理，不是逐字逐句的源码转录。

## 2. 命令类型

`/insights` 在二进制里被注册为一个 builtin `prompt` command。

可见线索：

- 命令名：`insights`
- 描述：`Generate a report analyzing your Claude Code sessions`
- 执行入口会先做本地 session 分析，再生成 HTML 报告和对话内摘要。

## 3. 数据来源

### 3.1 主数据目录

`/insights` 主要扫描：

- `~/.claude/projects`

这里按项目路径分目录，每个目录下包含 session 的 `.jsonl` 和同名目录。

### 3.2 缓存与派生数据

还会使用：

- `~/.claude/usage-data/session-meta`
- `~/.claude/usage-data/facets`

这些目录用于缓存 session 元信息与结构化 facets，避免每次都重算。

### 3.3 产物

最终会生成：

- `report.html`
- `report-zh.html`

并在对话里返回本地 `file://` 路径。

## 4. 完整执行流程

### Step 1. 注册为内建 prompt 命令

用户运行 `/insights` 后，命令进入内建 prompt 流程，而不是插件命令流程。

### Step 2. 扫描本地 session

扫描 `~/.claude/projects` 下的所有项目目录，收集 session 文件路径、修改时间、大小等信息。

### Step 3. 读取缓存 meta

优先尝试读取 session 元信息缓存。

如果缓存命中，直接使用。

如果缓存未命中，则回退读取原始 `.jsonl` transcript，并从中提取 session 级统计。

### Step 4. 去重与质量过滤

按 `session_id` 去重：

- 同一 `session_id` 若出现多个版本，优先保留用户消息更多的
- 如果消息数相同，则保留持续时间更长的

然后过滤掉过短样本：

- `user_message_count < 2` 的 session 丢弃
- `duration_minutes < 1` 的 session 丢弃

### Step 5. 长 session 先做分块摘要

如果 transcript 太长，不直接做 facets 抽取，而是先切块，对每块调用摘要 prompt，得到精简版上下文。

### Step 6. 单 session facets 抽取

对每个有效 session 调用 facets 抽取 prompt，得到结构化标签：

- 用户真实目标
- 会话结果
- 满意度信号
- friction 类型
- friction 细节说明
- Claude 帮助度
- session 类型

这里有一个重要修正：

- `goal_categories` 的具体键名是版本相关的，不能在文档里写成固定枚举
- 在本机当前产物里，真实样例能看到 `implement_plan`、`debug_issue` 这样的键
- 因此更准确的表述应该是“稳定对象结构 + 动态类别键”，而不是固定 category 列表

### Step 7. 过滤 warmup

如果某个 session 的 `goal_categories` 只有 `warmup_minimal`，会从正式统计中排除，避免把热身/试探会话算进去。

### Step 8. 聚合统计

聚合字段包括但不限于：

- `total_sessions`
- `total_messages`
- `total_duration_hours`
- `total_input_tokens`
- `total_output_tokens`
- `tool_counts`
- `languages`
- `git_commits`
- `git_pushes`
- `projects`
- `goal_categories`
- `outcomes`
- `satisfaction`
- `helpfulness`
- `session_types`
- `friction`
- `success`
- `total_interruptions`
- `total_tool_errors`
- `tool_error_categories`
- `user_response_times`
- `sessions_using_task_agent`
- `sessions_using_mcp`
- `sessions_using_web_search`
- `sessions_using_web_fetch`
- `total_lines_added`
- `total_lines_removed`
- `total_files_modified`
- `days_active`
- `messages_per_day`
- `message_hours`
- `multi_clauding`

其中 `multi_clauding` 会基于不同 session 的用户消息时间戳重叠情况做并行使用检测。

### Step 9. 生成报告各 section

在聚合数据上继续调用多个报告 prompt，生成：

- `project_areas`
- `interaction_style`
- `what_works`
- `friction_analysis`
- `suggestions`
- `on_the_horizon`
- `fun_ending`

结合本机实际产出的 [report.html](/Users/hugo/.claude/usage-data/report.html)，最终展示层至少包含这些页面 section：

- `At a Glance`
- `What You Work On`
- `How You Use Claude Code`
- `Impressive Things You Did`
- `Where Things Go Wrong`
- `Existing CC Features to Try`
- `New Ways to Use Claude Code`
- `On the Horizon`
- `fun-ending` 记忆点卡片

另外在当前 HTML 导航里还能看到 `section-feedback` 锚点，但这份文档不把它写成稳定功能，因为仅凭当前产物还不足以确认它在所有版本里都是完整 section。

### Step 10. 生成 At a Glance

再把上述 section 和聚合统计压缩成一个首页摘要：

- `whats_working`
- `whats_hindering`
- `quick_wins`
- `ambitious_workflows`

### Step 11. 渲染 HTML 报告

将聚合数据和各 section 组合成 `Claude Code Insights` HTML 页面。

### Step 12. 包装最终对话响应

最后把：

- insights JSON
- report URL
- HTML file path
- facets 目录
- 用户最终看到的摘要文案

一起交给一个最终包装 prompt，并要求主模型输出固定文案。

## 5. Prompt Pipeline

完整链路可概括为：

```text
/insights
-> scan ~/.claude/projects
-> load session meta cache
-> if transcript too long: chunk summarization
-> per-session facets extraction
-> filter warmup_minimal
-> aggregate metrics
-> generate report sections
-> generate at_a_glance
-> render report.html
-> final wrapper message
```

## 6. 忠实重建版提示词

下面不是二进制里的逐字全文，而是基于提取片段恢复后的完整逻辑模板。

### 6.1 Chunk Summary Prompt

用途：长 transcript 预摘要。

重建模板：

```text
Summarize this portion of a Claude Code session transcript.

Focus on:
1. What the user asked for
2. What Claude did, including tools used and files modified
3. Any friction, problems, or mistakes
4. The outcome

Constraints:
- Keep it concise
- Use roughly 3-5 sentences
- Preserve concrete details when available
- Keep file names, error messages, and user feedback if they are important

Input:
TRANSCRIPT CHUNK:
{chunk}
```

参考片段位置：

- `/tmp/claude-insights-prompts/chunk_summary_prompt.txt`

### 6.2 Session Facets Extraction Prompt

用途：把单个 session 结构化。

重建模板：

```text
Analyze this Claude Code session and extract structured facets.

Critical guidelines:
1. Count only goals the user explicitly asked for.
   - Do not count Claude's autonomous exploration
   - Do not count work Claude decided to do on its own
   - Count goals only when the user clearly asks

2. Infer satisfaction only from explicit user signals.
   Examples:
   - enthusiastic praise -> happy
   - “thanks”, “looks good”, “that works” -> satisfied
   - continuing smoothly without complaint -> likely_satisfied
   - “that’s not right”, “try again” -> dissatisfied
   - “this is broken”, “I give up” -> frustrated

3. Friction must be specific.
   Use categories such as:
   - misunderstood_request
   - wrong_approach
   - buggy_code
   - user_rejected_action
   - excessive_changes
   - wrong_file_or_location
   - tool_failed
   - external_issue

4. If the session is very short or just warmup, use warmup_minimal.

Return a valid JSON object with fields like:
- underlying_goal
- brief_summary
- goal_categories
- outcome
- user_satisfaction_counts
- claude_helpfulness
- session_type
- friction_counts
- friction_detail
- primary_success

SESSION:
{session_or_session_summary}
```

参考片段位置：

- `/tmp/claude-insights-prompts/facets_prompt.txt`

### 6.3 Project Areas Prompt

用途：识别用户在哪些项目区域使用 Claude。

重建模板：

```text
Analyze this Claude Code usage data and identify project areas.

Return only valid JSON:
{
  "areas": [
    {
      "name": "Area name",
      "session_count": N,
      "description": "2-3 sentences about what was worked on and how Claude Code was used."
    }
  ]
}

Requirements:
- Include 4-5 areas
- Skip internal Claude Code operations
```

参考片段位置：

- `/tmp/claude-insights-prompts/project_areas_prompt.txt`

### 6.4 Interaction Style Prompt

用途：总结用户的交互方式。

重建模板：

```text
Analyze this Claude Code usage data and describe the user's interaction style.

Return only valid JSON:
{
  "narrative": "2-3 paragraphs analyzing how the user interacts with Claude Code. Use second person 'you'. Describe patterns such as iterative refinement vs detailed upfront specs, interruptions vs letting Claude run, and include specific examples. Use bold for key insights.",
  "key_pattern": "One sentence summary of the most distinctive interaction style"
}
```

参考片段位置：

- `/tmp/claude-insights-prompts/interaction_style_prompt.txt`

### 6.5 What Works Prompt

用途：总结做得好的工作流。

重建模板：

```text
Analyze this Claude Code usage data and identify what's working well for this user.
Use second person ("you").

Return only valid JSON:
{
  "intro": "1 sentence of context",
  "impressive_workflows": [
    {
      "title": "Short title (3-6 words)",
      "description": "2-3 sentences describing the effective workflow or approach. Use 'you' not 'the user'."
    }
  ]
}

Requirements:
- Include 3 impressive workflows
```

参考片段位置：

- `/tmp/claude-insights-prompts/what_works_prompt_clean.txt`

### 6.6 Friction Analysis Prompt

用途：总结用户常见摩擦点。

重建模板：

```text
Analyze this Claude Code usage data and identify friction points for this user.
Use second person ("you").

Return only valid JSON:
{
  "intro": "1 sentence summarizing friction patterns",
  "categories": [
    {
      "category": "Concrete category name",
      "description": "1-2 sentences explaining the pattern and what could be done differently. Use 'you' not 'the user'.",
      "examples": [
        "Specific example with consequence",
        "Another example"
      ]
    }
  ]
}

Requirements:
- Include 3 friction categories
- Include 2 examples per category
```

参考片段位置：

- `/tmp/claude-insights-prompts/friction_and_suggestions_prompt_clean.txt`

### 6.7 Suggestions Prompt

用途：给出能力建议与使用建议。

内置能力参考包括：

- MCP Servers
- Custom Skills
- Hooks
- Headless Mode
- Task Agents

重建模板：

```text
Analyze this Claude Code usage data and suggest improvements.

Use the built-in Claude Code features reference as candidate suggestions.

Return only valid JSON:
{
  "claude_md_additions": [
    {
      "addition": "A line or block to add to CLAUDE.md",
      "why": "Why this would help based on actual sessions",
      "prompt_scaffold": "Where to put it in CLAUDE.md"
    }
  ],
  "features_to_try": [
    {
      "feature": "Feature name from the reference",
      "one_liner": "What it does",
      "why_for_you": "Why this would help you based on your sessions",
      "example_code": "A copyable command or config"
    }
  ],
  "usage_patterns": [
    {
      "title": "Short title",
      "suggestion": "1-2 sentence summary",
      "detail": "3-4 sentences explaining how this applies to your work",
      "copyable_prompt": "A prompt to try"
    }
  ]
}

Important rules:
- Prioritize CLAUDE.md additions that reflect instructions the user repeated across multiple sessions
- For features_to_try, choose 2-3 items from the built-in feature reference
- Include 2-3 items for each category
```

参考片段位置：

- `/tmp/claude-insights-prompts/friction_and_suggestions_prompt_clean.txt`

### 6.8 On the Horizon Prompt

用途：给出更强模型时代的未来工作流建议。

重建模板：

```text
Analyze this Claude Code usage data and identify future opportunities.

Return only valid JSON:
{
  "intro": "1 sentence about evolving AI-assisted development",
  "opportunities": [
    {
      "title": "Short title (4-8 words)",
      "whats_possible": "2-3 ambitious sentences about more autonomous workflows",
      "how_to_try": "1-2 sentences mentioning relevant tooling",
      "copyable_prompt": "A detailed prompt to try"
    }
  ]
}

Requirements:
- Include 3 opportunities
- Think big: autonomous workflows, parallel agents, iterative loops against tests, longer-running execution
```

参考片段位置：

- `/tmp/claude-insights-prompts/on_the_horizon_and_fun_prompt_clean.txt`

### 6.9 Fun Ending Prompt

用途：找一个人味比较强的收尾时刻。

重建模板：

```text
Analyze this Claude Code usage data and find a memorable moment.

Return only valid JSON:
{
  "headline": "A memorable qualitative moment from the transcripts, not a statistic",
  "detail": "Brief context about when or where it happened"
}

Find something genuinely interesting, funny, or surprising from the session summaries.
```

参考片段位置：

- `/tmp/claude-insights-prompts/on_the_horizon_and_fun_prompt_clean.txt`

### 6.10 At a Glance Prompt

用途：压缩成首页摘要。

重建模板：

```text
You're writing an "At a Glance" summary for a Claude Code usage insights report.
The goal is to help the user understand their usage and improve how they use Claude Code as models improve.

Use this 4-part structure:
1. What's working
2. What's hindering you
3. Quick wins to try
4. Ambitious workflows for better models

Constraints:
- Keep each section to 2-3 not-too-long sentences
- Do not overwhelm the user
- Do not focus on raw tool-call stats
- Do not be fluffy or overly complimentary
- Use a constructive coaching tone
- Avoid explicit numerical stats in the prose

Return only valid JSON:
{
  "whats_working": "...",
  "whats_hindering": "...",
  "quick_wins": "...",
  "ambitious_workflows": "..."
}

Session data available to this prompt includes:
- aggregated metrics
- project areas
- big wins
- friction categories
- features to try
- usage patterns
- on the horizon
```

参考片段位置：

- `/tmp/claude-insights-prompts/at_a_glance_prompt_clean.txt`

### 6.11 Final Wrapper Prompt

用途：把内部生成结果交给主对话。

重建模板：

```text
The user just ran /insights to generate a usage report analyzing their Claude Code sessions.

You are given:
- the full insights data JSON
- the report URL
- the HTML file path
- the facets directory
- the final report text the user would see

Now output exactly a short message that says:
- the shareable insights report is ready
- includes the local file URL
- asks whether the user wants to dig into a section or try one of the suggestions
```

参考片段位置：

- `/tmp/claude-insights-prompts/final_wrapper_prompt.txt`

## 7. 数据结构总览

### 7.1 Session Facets

基于当前真实产物，典型结构更接近：

```json
{
  "underlying_goal": "string",
  "goal_categories": {},
  "outcome": "fully_achieved|mostly_achieved|partially_achieved|not_achieved|unclear_from_transcript",
  "user_satisfaction_counts": {},
  "claude_helpfulness": "unhelpful|slightly_helpful|moderately_helpful|very_helpful|essential",
  "session_type": "string",
  "friction_counts": {},
  "friction_detail": "string",
  "primary_success": "string",
  "brief_summary": "string",
  "session_id": "uuid"
}
```

补充说明：

- `goal_categories` 是对象，但键名在版本之间可能演进
- 当前真实样例中可见的键包括 `implement_plan`、`debug_issue`
- 因此不要把这部分 schema 写成固定 enum

真实样例可见：

- [facets/38a8c627-7d57-4c88-94de-5744c1b11e30.json](/Users/hugo/.claude/usage-data/facets/38a8c627-7d57-4c88-94de-5744c1b11e30.json)

### 7.2 聚合数据

聚合结构包含：

```json
{
  "total_sessions": 0,
  "sessions_with_facets": 0,
  "date_range": {"start": "", "end": ""},
  "total_messages": 0,
  "total_duration_hours": 0,
  "total_input_tokens": 0,
  "total_output_tokens": 0,
  "tool_counts": {},
  "languages": {},
  "git_commits": 0,
  "git_pushes": 0,
  "projects": {},
  "goal_categories": {},
  "outcomes": {},
  "satisfaction": {},
  "helpfulness": {},
  "session_types": {},
  "friction": {},
  "success": {},
  "session_summaries": [],
  "total_interruptions": 0,
  "total_tool_errors": 0,
  "tool_error_categories": {},
  "user_response_times": [],
  "median_response_time": 0,
  "avg_response_time": 0,
  "sessions_using_task_agent": 0,
  "sessions_using_mcp": 0,
  "sessions_using_web_search": 0,
  "sessions_using_web_fetch": 0,
  "total_lines_added": 0,
  "total_lines_removed": 0,
  "total_files_modified": 0,
  "days_active": 0,
  "messages_per_day": 0,
  "message_hours": [],
  "multi_clauding": {
    "overlap_events": 0,
    "sessions_involved": 0,
    "user_messages_during": 0
  }
}
```

真实样例可见：

- [session-meta/38a8c627-7d57-4c88-94de-5744c1b11e30.json](/Users/hugo/.claude/usage-data/session-meta/38a8c627-7d57-4c88-94de-5744c1b11e30.json)

### 7.3 报告展示层

根据当前真实产物：

- 英文报告：[report.html](/Users/hugo/.claude/usage-data/report.html)
- 中文报告：[report-zh.html](/Users/hugo/.claude/usage-data/report-zh.html)

当前可以确认：

- `report.html` 是主英文报告
- `report-zh.html` 是本地化后的中文报告副本
- 两者共用同一套核心分析结果，但展示文案和部分 section 标题会本地化

英文报告中已确认的展示区块包括：

- `At a Glance`
- `What You Work On`
- `How You Use Claude Code`
- `Impressive Things You Did`
- `Where Things Go Wrong`
- `Existing CC Features to Try`
- `Suggested CLAUDE.md Additions`
- `New Ways to Use Claude Code`
- `On the Horizon`
- `fun-ending`

以及多种统计图卡，例如：

- `What You Wanted`
- `Top Tools Used`
- `Languages`
- `Session Types`
- `User Response Time Distribution`
- `Multi-Clauding (Parallel Sessions)`
- `Tool Errors Encountered`
- `Primary Friction Types`
- `Inferred Satisfaction (model-estimated)`

## 8. 最终结论

`/insights` 不是单条 prompt，而是一条多阶段分析流水线：

- 本地日志扫描
- transcript 压缩
- per-session 结构化抽取
- 全局聚合
- 多个分析 section 生成
- 总括摘要生成
- HTML 报告输出
- 对话内最终包装

它的核心特征不是“统计展示”，而是“用模型对历史 session 做二次理解，再把结构化分析和产品内置建议混合输出”。

## 9. OpenCortex 复刻实现建议

这一节的目标不是 1:1 复制 Claude Code 的内部实现，而是把它转成适合 OpenCortex 的可维护工程方案。

建议原则：

- 保留原始能力形状：`session-meta -> facets -> aggregate -> sections -> html`
- 弱化版本耦合：分类键、section 集合、prompt 模板都应配置化
- 优先做可重复运行的离线分析流程，再考虑在线触发与前端展示

### 9.1 建议的模块边界

可以拆成 6 个模块：

1. `session_source`
   负责扫描原始会话、读取 transcript、统一成内部 session 事件模型。

2. `session_meta_builder`
   从原始 transcript 抽取轻量统计，生成 `session-meta`。

3. `facet_extractor`
   负责长 session 压缩和单 session 结构化抽取。

4. `insights_aggregator`
   把 `session-meta` 和 `facets` 聚合成全局统计对象。

5. `insights_sections`
   跑多个 section prompt，生成 `project_areas`、`what_works` 等结构化结果。

6. `insights_renderer`
   负责输出 `report.html`、`report-zh.html`，以及 CLI/API 最终摘要消息。

### 9.2 建议的数据模型

#### 原始 session 输入

OpenCortex 内部需要先定义一个统一的 session 输入模型，至少包含：

```json
{
  "session_id": "uuid",
  "project_path": "string",
  "messages": [],
  "created_at": "iso-datetime",
  "updated_at": "iso-datetime"
}
```

其中 `messages` 至少要能区分：

- user
- assistant
- tool_use
- tool_result
- system

#### Session Meta

建议直接沿用 Claude Code 当前产物的风格：

```json
{
  "session_id": "uuid",
  "project_path": "string",
  "start_time": "iso-datetime",
  "duration_minutes": 0,
  "user_message_count": 0,
  "assistant_message_count": 0,
  "tool_counts": {},
  "languages": {},
  "git_commits": 0,
  "git_pushes": 0,
  "input_tokens": 0,
  "output_tokens": 0,
  "first_prompt": "string",
  "user_interruptions": 0,
  "user_response_times": [],
  "tool_errors": 0,
  "tool_error_categories": {},
  "uses_task_agent": false,
  "uses_mcp": false,
  "uses_web_search": false,
  "uses_web_fetch": false,
  "lines_added": 0,
  "lines_removed": 0,
  "files_modified": 0,
  "message_hours": [],
  "user_message_timestamps": []
}
```

#### Session Facets

建议保持“稳定骨架 + 动态键名”的设计：

```json
{
  "session_id": "uuid",
  "underlying_goal": "string",
  "brief_summary": "string",
  "goal_categories": {},
  "outcome": "string",
  "user_satisfaction_counts": {},
  "claude_helpfulness": "string",
  "session_type": "string",
  "friction_counts": {},
  "friction_detail": "string",
  "primary_success": "string"
}
```

关键建议：

- `goal_categories` 不要做成固定 enum 字段
- `friction_counts` 也不要耦合到前端硬编码
- 分类标签放到配置文件里，便于后续 prompt 调整

#### Aggregated Data

OpenCortex 侧建议保留与 Claude Code 近似的总览结构，因为它已经覆盖了报告统计所需的大部分维度。

### 9.3 建议的缓存策略

建议在 OpenCortex 中也采用两层缓存：

1. `session-meta` 缓存
   输入变化小、可快速重建，用于避免每次都重扫 transcript。

2. `facets` 缓存
   成本更高，因为涉及模型调用，应单独缓存。

推荐缓存键：

- `session_id`
- `source_digest`

其中 `source_digest` 可以基于 transcript 文件内容哈希、消息总数、最后更新时间组合生成。

推荐目录结构：

```text
opencortex-data/
  insights/
    session-meta/
      <session_id>.json
    facets/
      <session_id>.json
    reports/
      report.html
      report-zh.html
      report.json
```

这样做的好处是：

- session 级缓存可单独失效
- 报告可直接落地供前端或静态文件服务使用
- 后续支持多次生成不同版本报告也更容易

### 9.4 建议的执行 pipeline

建议实现成明确的阶段式任务，而不是一个超大函数。

```text
collect_sessions
-> build_or_load_session_meta
-> dedupe_sessions
-> filter_short_sessions
-> summarize_long_sessions
-> extract_or_load_facets
-> filter_warmup_like_sessions
-> aggregate_usage_data
-> generate_report_sections
-> generate_at_a_glance
-> render_report_assets
-> emit_cli_or_api_response
```

每一层都建议保留中间产物，这样利于：

- 断点续跑
- prompt 调试
- 线上问题排查
- 比较不同模型或 prompt 版本的输出差异

### 9.5 Prompt 设计建议

复刻时不要把所有逻辑塞进一个巨型 prompt，建议继续沿用多 prompt pipeline。

#### 建议保留的 prompt 颗粒度

- `chunk_summary`
- `facet_extraction`
- `project_areas`
- `interaction_style`
- `what_works`
- `friction_analysis`
- `suggestions`
- `on_the_horizon`
- `fun_ending`
- `at_a_glance`

#### 建议做成模板配置的部分

- category label 列表
- 可选 section 开关
- 本地化语言
- 输出 JSON schema
- section 标题
- HTML 主题与品牌风格

#### 建议放到代码而不是 prompt 的部分

- session 去重规则
- 过滤短 session 规则
- 并行检测算法
- 工具计数与语言计数
- token、时长、文件修改数等硬统计

也就是说：

- prompt 负责“解释和归因”
- 代码负责“计数和约束”

### 9.6 OpenCortex 中的聚合与前端展示建议

建议把最终报告拆成三层：

1. `report.json`
   给前端和 API 使用的结构化结果

2. `report.html`
   可直接打开分享的静态报告

3. `summary.txt` 或 API message
   用于 CLI 输出、聊天回复或通知消息

推荐前端 section：

- At a Glance
- What You Work On
- How You Use the System
- Big Wins
- Friction
- Existing Features to Try
- Suggested Policy or Memory Additions
- New Usage Patterns
- On the Horizon
- Fun Ending

推荐图表：

- Goals
- Top Tools
- Languages
- Session Types
- Response Time Distribution
- Parallel Session Usage
- Tool Error Distribution
- Primary Friction Types
- Satisfaction Distribution

### 9.7 与 OpenCortex 现有能力的结合点

如果 OpenCortex 已经有记忆、技能、工作流、agent 编排能力，最值得接入的点有：

#### 记忆系统

把 `claude_md_additions` 类似建议泛化成：

- memory policy additions
- workflow rules to persist
- repeated user constraints

也就是从“建议写入 CLAUDE.md”升级为“建议写入 OpenCortex 的持久规则层”。

#### Agent 系统

把 `on_the_horizon` 与真实 agent 使用数据接上：

- 哪些任务最适合并行 agent
- 哪些场景值得自动拆任务
- 哪些失败模式适合引入 reviewer agent

#### Prompt 管理

把这套 `/insights` prompt pipeline 放入统一的 prompt registry，支持：

- prompt versioning
- A/B 比较
- model override
- 本地化模板

### 9.8 MVP 建议

如果要在 OpenCortex 最快做出一个可用版，建议按 3 个里程碑推进。

#### MVP 1

只做：

- session scan
- session-meta
- facets extraction
- aggregate JSON
- 最简单 HTML

先不做：

- 多语言
- fun_ending
- CLAUDE.md additions
- fancy charts

#### MVP 2

加入：

- `what_works`
- `friction_analysis`
- `suggestions`
- `at_a_glance`
- 中文报告

#### MVP 3

加入：

- `interaction_style`
- `on_the_horizon`
- `fun_ending`
- 配置化 prompt registry
- 多报告版本对比
- Web UI / API 接口

### 9.9 工程实现上的关键注意点

- 不要把分类体系写死在代码里
- 不要让 prompt 直接承担硬统计工作
- transcript 很长时一定要先压缩，否则成本和稳定性都会变差
- 所有模型输出都要走 schema 校验
- section 失败时要允许部分降级，而不是整份报告失败
- 缓存要可单条失效，不能只能全量重建
- 本地化最好在 section 层做，而不是最终全文翻译

### 9.10 一个可执行的最小接口

OpenCortex 内部可以先定义一个离线任务接口：

```python
def generate_usage_insights(
    source_dir: str,
    output_dir: str,
    language: str = "en",
    force_rebuild: bool = False,
) -> dict:
    ...
```

建议返回：

```json
{
  "report_json_path": "string",
  "report_html_path": "string",
  "report_localized_html_path": "string",
  "session_count": 0,
  "analyzed_session_count": 0,
  "generated_sections": [],
  "cache_hits": {
    "session_meta": 0,
    "facets": 0
  }
}
```

### 9.11 推荐结论

如果 OpenCortex 要复刻 `/insights`，最合理的路径不是“照搬 HTML”，而是：

- 先复刻数据层
- 再复刻 prompt pipeline
- 最后再做展示层

也就是说，应该优先把它实现成一个可重复运行、可缓存、可验证的分析流水线，而不是一个一次性报告脚本。

## 10. 复刻可行性判断

这一节专门回答一个更直接的问题：

- OpenCortex 是否可以复刻 Claude Code `/insights`
- 是否能够输出“差不多”的 report
- 哪些部分可以追求接近，哪些部分应该接受差异

结论先行：

- 可以复刻
- 可以输出结构和体验都相近的 report
- 不应把目标设为逐字逐项 1:1 一致
- 更合理的目标是“同类能力、相近阅读体验、适配 OpenCortex 的数据模型与产品能力”

### 10.1 可以 1:1 接近复刻的部分

以下部分本质上是工程流水线设计，不依赖 Claude Code 独有前端，因此最适合直接复刻：

- 多阶段 pipeline 形状：`session-meta -> facets -> aggregate -> sections -> html`
- 两层缓存设计：`session-meta` 与 `facets`
- 长 transcript 先压缩、再做 facets 抽取
- 聚合后再生成多个 section，而不是用单个巨型 prompt 一次完成
- `At a Glance` 作为最终总括层
- 静态 HTML 报告输出

换句话说，报告的整体骨架、生成顺序和产物形态，都可以做得非常接近。

### 10.2 可以做得很像，但很难严格一致的部分

以下部分可以做得“效果接近”，但不应承诺与 Claude Code 产物完全一致：

- 各 section 的具体文案
- `goal_categories`、`friction`、`session_type` 等语义分类结果
- `what_works`、`friction_analysis`、`on_the_horizon` 这类解释型洞察
- `fun_ending` 这类带明显主观色彩的收尾内容
- 中文本地化后的措辞和标题

原因不是 HTML 或 prompt 写不出来，而是这些内容本质上依赖：

- 底层 session 数据是否完整
- 统计口径是否完全一致
- 使用的模型、prompt 版本、schema 约束是否一致
- Claude Code 内部是否还有本文档无法完全观察到的启发式规则

因此，OpenCortex 可以产出“同级别、同类型、同阅读价值”的 report，但不应把目标写成“官方 report 的逐项镜像”。

### 10.3 取决于 OpenCortex 数据面的部分

能否做出高质量 report，最关键的前提不是 HTML，而是 session 数据质量。

如果 OpenCortex 能稳定拿到这些信号，就可以把报告质量做上去：

- user / assistant / tool_use / tool_result / system 的事件边界
- 工具调用次数与错误类别
- 文件修改、增删行数、语言分布
- session 时长、用户响应时间、用户打断
- agent 使用、MCP 使用、web 搜索或抓取使用
- 用户消息时间戳，用于并行 session 检测

如果这些数据缺失，报告仍然能生成，但会退化为：

- 偏 transcript 摘要
- 硬统计维度减少
- 解释比计数多
- 图表可信度下降

所以从工程优先级上，应先把 session 事件模型打牢，再追求页面效果。

### 10.4 OpenCortex 应主动自定义的部分

即使目标是复刻 `/insights`，也不建议把 Claude Code 的设计原样照搬到底。

更适合 OpenCortex 主动自定义的部分包括：

- 分类键与 schema：保持稳定骨架，但允许动态标签
- 建议写入 `CLAUDE.md` 的能力：改造成 OpenCortex 的 memory / policy / workflow 持久层
- `Existing CC Features to Try`：替换成 OpenCortex 自己的功能清单
- `On the Horizon`：与 OpenCortex 的 agent orchestration、workflow、memory 结合
- 报告 section 开关、文案语言、HTML 主题：做成配置项

也就是说：

- Claude Code 的 `/insights` 可以作为能力模板
- OpenCortex 不应把自己限制成 Claude Code 的 UI 克隆

### 10.5 实际可达成的目标

如果按本文档第 9 节的方案推进，OpenCortex 实际上可以达到三个层次的结果。

#### Level 1: 可用复刻

具备：

- session scan
- session-meta
- facets
- aggregate JSON
- 基础 HTML 报告

这个阶段已经能回答“用户做了什么、常见摩擦在哪、有哪些值得尝试的工作流”。

#### Level 2: 高相似度复刻

再加入：

- `what_works`
- `friction_analysis`
- `suggestions`
- `at_a_glance`
- 多图表与中文报告

这个阶段的用户感知会与 Claude Code `/insights` 很接近，已经可以说是“差不多的 report”。

#### Level 3: OpenCortex 化增强版

继续加入：

- `interaction_style`
- `on_the_horizon`
- `fun_ending`
- prompt registry
- memory / policy 建议写回
- agent 与 workflow 的真实使用反馈

这个阶段就不只是复刻，而是把 `/insights` 升级成 OpenCortex 自己的 usage intelligence 系统。

### 10.6 最终判断

如果问题是：

- “OpenCortex 能不能复刻 `/insights`？”

答案是：

- 能

如果问题是：

- “能不能输出差不多的 report？”

答案也是：

- 能

但这里的“差不多”应理解为：

- 结构接近
- 统计维度接近
- section 类型接近
- 阅读体验接近
- 产品建议与洞察价值接近

而不是：

- 文案逐字一致
- 分类体系完全一致
- 每个统计数字与官方实现永久一致

因此，OpenCortex 复刻 `/insights` 的目标应该定义为：

- 复刻其分析流水线与报告能力形状
- 逼近其报告完成度和可读性
- 在 OpenCortex 自己的记忆、技能、agent、workflow 体系上做进一步增强
