# `/insights` 命令完整调查报告（含全部提示词原文）

> 源码位置: `src/commands/insights.ts`（3200 行, 113KB）
> 入口垫片: `src/commands.ts:188-201`（懒加载）
> 数据源工具: `src/utils/sessionStorage.ts`

---

## 1. 概述

`/insights` 是 Claude Code 的内置斜杠命令，用于生成用户使用 Claude Code 的分析报告。入口在 `src/commands.ts:188-201`（懒加载垫片，延迟 import 113KB 模块），实现在 `src/commands/insights.ts`（约 3200 行）。

报告最终输出为一个**可分享的 HTML 文件**，保存在 `~/.claude/usage-data/report.html`。

命令注册：

```typescript
// src/commands.ts:188-201
// insights.ts is 113KB (3200 lines, includes diffLines/html rendering). Lazy
// shim defers the heavy module until /insights is actually invoked.
const usageReport: Command = {
  type: 'prompt',
  name: 'insights',
  description: 'Generate a report analyzing your Claude Code sessions',
  contentLength: 0,
  progressMessage: 'analyzing your sessions',
  source: 'builtin',
  async getPromptForCommand(args, context) {
    const real = (await import('./commands/insights.js')).default
    if (real.type !== 'prompt') throw new Error('unreachable')
    return real.getPromptForCommand(args, context)
  },
}
```

---

## 2. 数据源

### 2.1 本地 session 日志（主数据源）

| 路径 | 格式 | 说明 |
|---|---|---|
| `~/.claude/projects/*/*.jsonl` | JSONL | 每个 session 的完整消息记录 |
| `~/.claude/usage-data/session-meta/{id}.json` | JSON | SessionMeta 缓存 |
| `~/.claude/usage-data/facets/{id}.json` | JSON | SessionFacets 缓存（LLM 提取） |

关键函数（`src/utils/sessionStorage.ts`）：
- `getProjectsDir()` → `~/.claude/projects`
- `getSessionFilesWithMtime(dir)` → 扫描 `.jsonl` 文件 + stat（sessionId → {path, mtime, ctime, size}）
- `loadAllLogsFromSessionFile(path)` → 解析 JSONL 为 `LogOption[]`
- `getSessionIdFromLog(log)` → 提取 session ID

### 2.2 远程 homespace 数据（仅 `USER_TYPE === 'ant'`）

通过 `coder list -o json` 获取运行中的 workspace，再 `scp` 拉取 `/root/.claude/projects/` 目录。使用 `--homespaces` 参数启用。

### 2.3 SessionMeta 提取（纯代码统计，不调用 LLM）

`extractToolStats()` 函数（约 250 行）从消息日志中提取：

| 指标 | 提取方式 |
|---|---|
| 工具使用次数 | 遍历 `tool_use` block，计数每个 `name` |
| 编程语言 | 文件扩展名映射（`.ts`→TypeScript 等 16 种） |
| Git 操作 | `command` 字段包含 `git commit` / `git push` |
| Token 用量 | `message.usage.input_tokens` / `output_tokens` |
| 行数变化 | `Edit` 用 `diffLines(old, new)`；`Write` 按换行符计数 |
| 用户中断 | 检测 `[Request interrupted by user` 文本 |
| 响应时间 | assistant 到 user 的时间差（2s~3600s 范围） |
| 工具错误 | `tool_result.is_error` + 内容关键词分类 |
| 消息时间 | `new Date(timestamp).getHours()` |
| 特殊工具 | Agent/MCP/WebSearch/WebFetch 使用标记 |
| 修改文件数 | Edit/Write 工具涉及的唯一 file_path |

#### SessionMeta 完整类型定义

```typescript
type SessionMeta = {
  session_id: string
  project_path: string
  start_time: string
  duration_minutes: number
  user_message_count: number
  assistant_message_count: number
  tool_counts: Record<string, number>
  languages: Record<string, number>
  git_commits: number
  git_pushes: number
  input_tokens: number
  output_tokens: number
  first_prompt: string
  summary?: string
  user_interruptions: number
  user_response_times: number[]
  tool_errors: number
  tool_error_categories: Record<string, number>
  uses_task_agent: boolean
  uses_mcp: boolean
  uses_web_search: boolean
  uses_web_fetch: boolean
  lines_added: number
  lines_removed: number
  files_modified: number
  message_hours: number[]
  user_message_timestamps: string[]
}
```

#### SessionFacets 完整类型定义（LLM 提取结果）

```typescript
type SessionFacets = {
  session_id: string
  underlying_goal: string
  goal_categories: Record<string, number>
  outcome: string
  user_satisfaction_counts: Record<string, number>
  claude_helpfulness: string
  session_type: string
  friction_counts: Record<string, number>
  friction_detail: string
  primary_success: string
  brief_summary: string
  user_instructions_to_claude?: string[]
}
```

#### AggregatedData 完整类型定义（聚合后数据）

```typescript
type AggregatedData = {
  total_sessions: number
  total_sessions_scanned?: number
  sessions_with_facets: number
  date_range: { start: string; end: string }
  total_messages: number
  total_duration_hours: number
  total_input_tokens: number
  total_output_tokens: number
  tool_counts: Record<string, number>
  languages: Record<string, number>
  git_commits: number
  git_pushes: number
  projects: Record<string, number>
  goal_categories: Record<string, number>
  outcomes: Record<string, number>
  satisfaction: Record<string, number>
  helpfulness: Record<string, number>
  session_types: Record<string, number>
  friction: Record<string, number>
  success: Record<string, number>
  session_summaries: Array<{ id: string; date: string; summary: string; goal?: string }>
  total_interruptions: number
  total_tool_errors: number
  tool_error_categories: Record<string, number>
  user_response_times: number[]
  median_response_time: number
  avg_response_time: number
  sessions_using_task_agent: number
  sessions_using_mcp: number
  sessions_using_web_search: number
  sessions_using_web_fetch: number
  total_lines_added: number
  total_lines_removed: number
  total_files_modified: number
  days_active: number
  messages_per_day: number
  message_hours: number[]
  multi_clauding: {
    overlap_events: number
    sessions_involved: number
    user_messages_during: number
  }
}
```

#### 工具错误分类逻辑

```typescript
if (lowerContent.includes('exit code'))                    → 'Command Failed'
if (lowerContent.includes('rejected') || 'doesn\'t want') → 'User Rejected'
if (lowerContent.includes('string to replace not found'))  → 'Edit Failed'
if (lowerContent.includes('modified since read'))          → 'File Changed'
if (lowerContent.includes('exceeds maximum'))              → 'File Too Large'
if (lowerContent.includes('file not found'))               → 'File Not Found'
else                                                       → 'Other'
```

#### 编程语言映射表

```typescript
const EXTENSION_TO_LANGUAGE: Record<string, string> = {
  '.ts': 'TypeScript', '.tsx': 'TypeScript',
  '.js': 'JavaScript', '.jsx': 'JavaScript',
  '.py': 'Python', '.rb': 'Ruby', '.go': 'Go',
  '.rs': 'Rust', '.java': 'Java', '.md': 'Markdown',
  '.json': 'JSON', '.yaml': 'YAML', '.yml': 'YAML',
  '.sh': 'Shell', '.css': 'CSS', '.html': 'HTML',
}
```

#### 标签映射表（`LABEL_MAP`）

```typescript
const LABEL_MAP: Record<string, string> = {
  // Goal categories
  debug_investigate: 'Debug/Investigate',
  implement_feature: 'Implement Feature',
  fix_bug: 'Fix Bug',
  write_script_tool: 'Write Script/Tool',
  refactor_code: 'Refactor Code',
  configure_system: 'Configure System',
  create_pr_commit: 'Create PR/Commit',
  analyze_data: 'Analyze Data',
  understand_codebase: 'Understand Codebase',
  write_tests: 'Write Tests',
  write_docs: 'Write Docs',
  deploy_infra: 'Deploy/Infra',
  warmup_minimal: 'Cache Warmup',
  // Success factors
  fast_accurate_search: 'Fast/Accurate Search',
  correct_code_edits: 'Correct Code Edits',
  good_explanations: 'Good Explanations',
  proactive_help: 'Proactive Help',
  multi_file_changes: 'Multi-file Changes',
  handled_complexity: 'Multi-file Changes',
  good_debugging: 'Good Debugging',
  // Friction types
  misunderstood_request: 'Misunderstood Request',
  wrong_approach: 'Wrong Approach',
  buggy_code: 'Buggy Code',
  user_rejected_action: 'User Rejected Action',
  claude_got_blocked: 'Claude Got Blocked',
  user_stopped_early: 'User Stopped Early',
  wrong_file_or_location: 'Wrong File/Location',
  excessive_changes: 'Excessive Changes',
  slow_or_verbose: 'Slow/Verbose',
  tool_failed: 'Tool Failed',
  user_unclear: 'User Unclear',
  external_issue: 'External Issue',
  // Satisfaction labels
  frustrated: 'Frustrated', dissatisfied: 'Dissatisfied',
  likely_satisfied: 'Likely Satisfied', satisfied: 'Satisfied',
  happy: 'Happy', unsure: 'Unsure', neutral: 'Neutral', delighted: 'Delighted',
  // Session types
  single_task: 'Single Task', multi_task: 'Multi Task',
  iterative_refinement: 'Iterative Refinement',
  exploration: 'Exploration', quick_question: 'Quick Question',
  // Outcomes
  fully_achieved: 'Fully Achieved', mostly_achieved: 'Mostly Achieved',
  partially_achieved: 'Partially Achieved', not_achieved: 'Not Achieved',
  unclear_from_transcript: 'Unclear',
  // Helpfulness
  unhelpful: 'Unhelpful', slightly_helpful: 'Slightly Helpful',
  moderately_helpful: 'Moderately Helpful', very_helpful: 'Very Helpful',
  essential: 'Essential',
}
```

---

## 3. 三阶段处理管道

### Phase 1: 轻量扫描（`scanAllSessions`）

- 仅读取文件系统元数据（mtime, size），不解析 JSONL
- 遍历 `~/.claude/projects/` 下所有子目录
- 每 10 个目录 yield 一次事件循环
- 按 mtime 降序排列

### Phase 2: SessionMeta 加载

- 按 50 个一批检查缓存（`~/.claude/usage-data/session-meta/{id}.json`）
- 未缓存的按 10 个一批解析 JSONL → `logToSessionMeta()` → 写入缓存
- 上限 `MAX_SESSIONS_TO_LOAD = 200`
- 去重：相同 `session_id` 保留用户消息最多的分支（`deduplicateSessionBranches`）
- 过滤 meta-session（自身 API 调用日志，检测 `RESPOND WITH ONLY A VALID JSON OBJECT` 或 `record_facets`）
- 过滤非实质 session：`user_message_count < 2` 或 `duration_minutes < 1`

### Phase 3: Facet 提取（LLM 驱动）

- 先并行检查所有缓存（`~/.claude/usage-data/facets/{id}.json`）
- 未缓存的：调用 Opus 模型提取 facets
- 上限 `MAX_FACET_EXTRACTIONS = 50`，并发 `CONCURRENCY = 50`
- 提取后缓存写入磁盘
- 最后过滤仅有 `warmup_minimal` 目标的 session

### Multi-clauding 检测算法（`detectMultiClauding`）

使用滑动窗口（30 分钟）检测并发会话模式 `session1 → session2 → session1`：
- 收集所有 session 的 user_message_timestamps
- 全局按时间排序
- 滑动窗口内检测同一 session 被其他 session 的消息"打断"的模式
- 返回：overlap_events, sessions_involved, user_messages_during

---

## 4. 全部提示词原文

### 4.1 Facet 提取提示词（`FACET_EXTRACTION_PROMPT` + JSON schema 后缀）

**模型**: Opus（`getDefaultOpusModel()`）
**maxOutputTokens**: 4096
**调用函数**: `extractFacetsFromAPI()`
**触发**: 每个未缓存的实质 session

完整提示词 = `FACET_EXTRACTION_PROMPT` + 转录文本 + JSON schema：

```
Analyze this Claude Code session and extract structured facets.

CRITICAL GUIDELINES:

1. **goal_categories**: Count ONLY what the USER explicitly asked for.
   - DO NOT count Claude's autonomous codebase exploration
   - DO NOT count work Claude decided to do on its own
   - ONLY count when user says "can you...", "please...", "I need...", "let's..."

2. **user_satisfaction_counts**: Base ONLY on explicit user signals.
   - "Yay!", "great!", "perfect!" → happy
   - "thanks", "looks good", "that works" → satisfied
   - "ok, now let's..." (continuing without complaint) → likely_satisfied
   - "that's not right", "try again" → dissatisfied
   - "this is broken", "I give up" → frustrated

3. **friction_counts**: Be specific about what went wrong.
   - misunderstood_request: Claude interpreted incorrectly
   - wrong_approach: Right goal, wrong solution method
   - buggy_code: Code didn't work correctly
   - user_rejected_action: User said no/stop to a tool call
   - excessive_changes: Over-engineered or changed too much

4. If very short or just warmup, use warmup_minimal for goal_category

SESSION:
{transcript — 由 formatTranscriptWithSummarization() 生成}

RESPOND WITH ONLY A VALID JSON OBJECT matching this schema:
{
  "underlying_goal": "What the user fundamentally wanted to achieve",
  "goal_categories": {"category_name": count, ...},
  "outcome": "fully_achieved|mostly_achieved|partially_achieved|not_achieved|unclear_from_transcript",
  "user_satisfaction_counts": {"level": count, ...},
  "claude_helpfulness": "unhelpful|slightly_helpful|moderately_helpful|very_helpful|essential",
  "session_type": "single_task|multi_task|iterative_refinement|exploration|quick_question",
  "friction_counts": {"friction_type": count, ...},
  "friction_detail": "One sentence describing friction or empty",
  "primary_success": "none|fast_accurate_search|correct_code_edits|good_explanations|proactive_help|multi_file_changes|good_debugging",
  "brief_summary": "One sentence: what user wanted and whether they got it"
}
```

#### 转录格式化（`formatTranscriptForFacets`）

```
Session: {session_id前8字符}
Date: {start_time}
Project: {project_path}
Duration: {duration} min

[User]: {user文本，截断500字符}
[Assistant]: {assistant文本，截断300字符}
[Tool: {tool_name}]
...
```

- 若转录 ≤ 30,000 字符，直接使用
- 若 > 30,000 字符，按 25,000 字符分块并行摘要后拼接

---

### 4.2 长转录分块摘要提示词（`SUMMARIZE_CHUNK_PROMPT`）

**模型**: Opus
**maxOutputTokens**: 500
**触发条件**: 单个 session 转录 > 30,000 字符时，按 25,000 字符分块

```
Summarize this portion of a Claude Code session transcript. Focus on:
1. What the user asked for
2. What Claude did (tools used, files modified)
3. Any friction or issues
4. The outcome

Keep it concise - 3-5 sentences. Preserve specific details like file names, error messages, and user feedback.

TRANSCRIPT CHUNK:
{chunk — 25000字符的转录片段}
```

摘要后拼接格式：
```
Session: {id}
Date: {start_time}
Project: {project_path}
Duration: {duration} min
[Long session - {N} parts summarized]

{summary_1}

---

{summary_2}

---

{summary_N}
```

---

### 4.3 Insight Section 1: `project_areas`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and identify project areas.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "areas": [
    {"name": "Area name", "session_count": N, "description": "2-3 sentences about what was worked on and how Claude Code was used."}
  ]
}

Include 4-5 areas. Skip internal CC operations.

DATA:
{dataContext}
```

---

### 4.4 Insight Section 2: `interaction_style`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and describe the user's interaction style.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "narrative": "2-3 paragraphs analyzing HOW the user interacts with Claude Code. Use second person 'you'. Describe patterns: iterate quickly vs detailed upfront specs? Interrupt often or let Claude run? Include specific examples. Use **bold** for key insights.",
  "key_pattern": "One sentence summary of most distinctive interaction style"
}

DATA:
{dataContext}
```

---

### 4.5 Insight Section 3: `what_works`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and identify what's working well for this user. Use second person ("you").

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "intro": "1 sentence of context",
  "impressive_workflows": [
    {"title": "Short title (3-6 words)", "description": "2-3 sentences describing the impressive workflow or approach. Use 'you' not 'the user'."}
  ]
}

Include 3 impressive workflows.

DATA:
{dataContext}
```

---

### 4.6 Insight Section 4: `friction_analysis`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and identify friction points for this user. Use second person ("you").

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "intro": "1 sentence summarizing friction patterns",
  "categories": [
    {"category": "Concrete category name", "description": "1-2 sentences explaining this category and what could be done differently. Use 'you' not 'the user'.", "examples": ["Specific example with consequence", "Another example"]}
  ]
}

Include 3 friction categories with 2 examples each.

DATA:
{dataContext}
```

---

### 4.7 Insight Section 5: `suggestions`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and suggest improvements.

## CC FEATURES REFERENCE (pick from these for features_to_try):
1. **MCP Servers**: Connect Claude to external tools, databases, and APIs via Model Context Protocol.
   - How to use: Run `claude mcp add <server-name> -- <command>`
   - Good for: database queries, Slack integration, GitHub issue lookup, connecting to internal APIs

2. **Custom Skills**: Reusable prompts you define as markdown files that run with a single /command.
   - How to use: Create `.claude/skills/commit/SKILL.md` with instructions. Then type `/commit` to run it.
   - Good for: repetitive workflows - /commit, /review, /test, /deploy, /pr, or complex multi-step workflows

3. **Hooks**: Shell commands that auto-run at specific lifecycle events.
   - How to use: Add to `.claude/settings.json` under "hooks" key.
   - Good for: auto-formatting code, running type checks, enforcing conventions

4. **Headless Mode**: Run Claude non-interactively from scripts and CI/CD.
   - How to use: `claude -p "fix lint errors" --allowedTools "Edit,Read,Bash"`
   - Good for: CI/CD integration, batch code fixes, automated reviews

5. **Task Agents**: Claude spawns focused sub-agents for complex exploration or parallel work.
   - How to use: Claude auto-invokes when helpful, or ask "use an agent to explore X"
   - Good for: codebase exploration, understanding complex systems

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "claude_md_additions": [
    {"addition": "A specific line or block to add to CLAUDE.md based on workflow patterns. E.g., 'Always run tests after modifying auth-related files'", "why": "1 sentence explaining why this would help based on actual sessions", "prompt_scaffold": "Instructions for where to add this in CLAUDE.md. E.g., 'Add under ## Testing section'"}
  ],
  "features_to_try": [
    {"feature": "Feature name from CC FEATURES REFERENCE above", "one_liner": "What it does", "why_for_you": "Why this would help YOU based on your sessions", "example_code": "Actual command or config to copy"}
  ],
  "usage_patterns": [
    {"title": "Short title", "suggestion": "1-2 sentence summary", "detail": "3-4 sentences explaining how this applies to YOUR work", "copyable_prompt": "A specific prompt to copy and try"}
  ]
}

IMPORTANT for claude_md_additions: PRIORITIZE instructions that appear MULTIPLE TIMES in the user data. If user told Claude the same thing in 2+ sessions (e.g., 'always run tests', 'use TypeScript'), that's a PRIME candidate - they shouldn't have to repeat themselves.

IMPORTANT for features_to_try: Pick 2-3 from the CC FEATURES REFERENCE above. Include 2-3 items for each category.

DATA:
{dataContext}
```

---

### 4.8 Insight Section 6: `on_the_horizon`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and identify future opportunities.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "intro": "1 sentence about evolving AI-assisted development",
  "opportunities": [
    {"title": "Short title (4-8 words)", "whats_possible": "2-3 ambitious sentences about autonomous workflows", "how_to_try": "1-2 sentences mentioning relevant tooling", "copyable_prompt": "Detailed prompt to try"}
  ]
}

Include 3 opportunities. Think BIG - autonomous workflows, parallel agents, iterating against tests.

DATA:
{dataContext}
```

---

### 4.9 Insight Section 7: `cc_team_improvements`（仅 `USER_TYPE === 'ant'`）

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and suggest product improvements for the CC team.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "improvements": [
    {"title": "Product/tooling improvement", "detail": "3-4 sentences describing the improvement", "evidence": "3-4 sentences with specific session examples"}
  ]
}

Include 2-3 improvements based on friction patterns observed.

DATA:
{dataContext}
```

---

### 4.10 Insight Section 8: `model_behavior_improvements`（仅 `USER_TYPE === 'ant'`）

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and suggest model behavior improvements.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "improvements": [
    {"title": "Model behavior change", "detail": "3-4 sentences describing what the model should do differently", "evidence": "3-4 sentences with specific examples"}
  ]
}

Include 2-3 improvements based on friction patterns observed.

DATA:
{dataContext}
```

---

### 4.11 Insight Section 9: `fun_ending`

**模型**: Opus | **maxOutputTokens**: 8192 | **并行执行**

```
Analyze this Claude Code usage data and find a memorable moment.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "headline": "A memorable QUALITATIVE moment from the transcripts - not a statistic. Something human, funny, or surprising.",
  "detail": "Brief context about when/where this happened"
}

Find something genuinely interesting or amusing from the session summaries.

DATA:
{dataContext}
```

---

### 4.12 At a Glance 提示词（串行执行，依赖前面所有 section 的输出）

**模型**: Opus | **maxOutputTokens**: 8192 | **串行**（等待 section 1-9 完成后执行）

```
You're writing an "At a Glance" summary for a Claude Code usage insights report for Claude Code users. The goal is to help them understand their usage and improve how they can use Claude better, especially as models improve.

Use this 4-part structure:

1. **What's working** - What is the user's unique style of interacting with Claude and what are some impactful things they've done? You can include one or two details, but keep it high level since things might not be fresh in the user's memory. Don't be fluffy or overly complimentary. Also, don't focus on the tool calls they use.

2. **What's hindering you** - Split into (a) Claude's fault (misunderstandings, wrong approaches, bugs) and (b) user-side friction (not providing enough context, environment issues -- ideally more general than just one project). Be honest but constructive.

3. **Quick wins to try** - Specific Claude Code features they could try from the examples below, or a workflow technique if you think it's really compelling. (Avoid stuff like "Ask Claude to confirm before taking actions" or "Type out more context up front" which are less compelling.)

4. **Ambitious workflows for better models** - As we move to much more capable models over the next 3-6 months, what should they prepare for? What workflows that seem impossible now will become possible? Draw from the appropriate section below.

Keep each section to 2-3 not-too-long sentences. Don't overwhelm the user. Don't mention specific numerical stats or underlined_categories from the session data below. Use a coaching tone.

RESPOND WITH ONLY A VALID JSON OBJECT:
{
  "whats_working": "(refer to instructions above)",
  "whats_hindering": "(refer to instructions above)",
  "quick_wins": "(refer to instructions above)",
  "ambitious_workflows": "(refer to instructions above)"
}

SESSION DATA:
{fullContext — 聚合 JSON + session summaries + friction details + user instructions}

## Project Areas (what user works on)
{projectAreasText — 来自 section 1 project_areas 的输出，格式: "- AreaName: description"}

## Big Wins (impressive accomplishments)
{bigWinsText — 来自 section 3 what_works 的输出，格式: "- Title: description"}

## Friction Categories (where things go wrong)
{frictionText — 来自 section 4 friction_analysis 的输出，格式: "- Category: description"}

## Features to Try
{featuresText — 来自 section 5 suggestions.features_to_try 的输出，格式: "- Feature: one_liner"}

## Usage Patterns to Adopt
{patternsText — 来自 section 5 suggestions.usage_patterns 的输出，格式: "- Title: suggestion"}

## On the Horizon (ambitious workflows for better models)
{horizonText — 来自 section 6 on_the_horizon 的输出，格式: "- Title: whats_possible"}
```

---

### 4.13 最终命令返回提示词（给 Claude 主对话的指令）

报告生成完毕后，作为 `getPromptForCommand` 的返回值传递给 Claude 主对话：

```
The user just ran /insights to generate a usage report analyzing their Claude Code sessions.

Here is the full insights data:
{JSON.stringify(insights, null, 2)}

Report URL: {reportUrl}
HTML file: {htmlPath}
Facets directory: {facetsDir}

Here is what the user sees:
{userSummary — markdown 格式的 At a Glance 摘要}

Now output the following message exactly:

<message>
Your shareable insights report is ready:
{reportUrl}{uploadHint}

Want to dig into any section or try one of the suggestions?
</message>
```

其中 `userSummary` 格式为：

```markdown
# Claude Code Insights

{sessions数} sessions · {messages数} messages · {hours}h · {commits} commits
{start_date} to {end_date}

## At a Glance

**What's working:** {whats_working} See _Impressive Things You Did_.

**What's hindering you:** {whats_hindering} See _Where Things Go Wrong_.

**Quick wins to try:** {quick_wins} See _Features to Try_.

**Ambitious workflows:** {ambitious_workflows} See _On the Horizon_.

Your full shareable insights report is ready: {reportUrl}
```

---

### 4.14 数据上下文构建（`dataContext` 完整结构）

所有 insight section（4.3-4.11）接收的 `DATA` 部分由以下内容拼接：

```
{                                          ← JSON 部分（jsonStringify 格式化）
  "sessions": 总session数,
  "analyzed": 有facet的session数,
  "date_range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "messages": 总消息数,
  "hours": 总时长(小时，取整),
  "commits": git提交数,
  "top_tools": [["Read", 500], ["Edit", 300], ...],   ← top 8, 按 count 降序
  "top_goals": [["implement_feature", 20], ...],        ← top 8, 按 count 降序
  "outcomes": {"fully_achieved": N, "mostly_achieved": N, ...},
  "satisfaction": {"satisfied": N, "happy": N, ...},
  "friction": {"wrong_approach": N, "buggy_code": N, ...},
  "success": {"correct_code_edits": N, ...},
  "languages": {"TypeScript": N, "Python": N, ...}
}

SESSION SUMMARIES:                          ← 最多 50 条，来自 facets.brief_summary
- User wanted X and achieved it (fully_achieved, very_helpful)
- User tried to fix Y but got stuck (partially_achieved, moderately_helpful)
...

FRICTION DETAILS:                           ← 最多 20 条，来自 facets.friction_detail
- Claude misunderstood the request and edited the wrong file
- Wrong approach to solving the database migration
...

USER INSTRUCTIONS TO CLAUDE:                ← 最多 15 条，来自 facets.user_instructions_to_claude
- Always run tests after changes
- Use TypeScript, not JavaScript
...（若无则显示 "None captured"）
```

---

## 5. HTML 报告输出模板

### 5.1 完整页面结构

```
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Claude Code Insights</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>{CSS — 约 120 行}</style>
</head>
<body>
  <div class="container">

    ┌─ <h1> "Claude Code Insights"
    ├─ <p class="subtitle"> "{N} messages across {N} sessions | {date range}"
    │
    ├─ At a Glance (金色渐变框 .at-a-glance)
    │   ├─ What's working → <a href="#section-wins">Impressive Things You Did →</a>
    │   ├─ What's hindering → <a href="#section-friction">Where Things Go Wrong →</a>
    │   ├─ Quick wins → <a href="#section-features">Features to Try →</a>
    │   └─ Ambitious workflows → <a href="#section-horizon">On the Horizon →</a>
    │
    ├─ <nav class="nav-toc"> 8 个锚点标签链接
    │   What You Work On | How You Use CC | Impressive Things |
    │   Where Things Go Wrong | Features to Try | New Usage Patterns |
    │   On the Horizon | Team Feedback
    │
    ├─ 统计行 (.stats-row)
    │   Messages | +Lines/-Lines | Files | Days | Msgs/Day
    │
    ├─ <h2 #section-work> "What You Work On"
    │   └─ 4-5 个 .project-area 卡片（白色背景）
    │       ├─ .area-name + .area-count ("~N sessions")
    │       └─ .area-desc
    │
    ├─ Charts Row 1 (.charts-row, 2列 grid)
    │   ├─ "What You Wanted" — generateBarChart(goal_categories, '#2563eb') 蓝色
    │   └─ "Top Tools Used" — generateBarChart(tool_counts, '#0891b2') 青色
    │
    ├─ Charts Row 2
    │   ├─ "Languages" — generateBarChart(languages, '#10b981') 绿色
    │   └─ "Session Types" — generateBarChart(session_types, '#8b5cf6') 紫色
    │
    ├─ <h2 #section-usage> "How You Use Claude Code"
    │   └─ .narrative（白色背景）
    │       ├─ markdownToHtml(narrative) — **bold** 转 <strong>
    │       └─ .key-insight（绿色背景高亮框）
    │
    ├─ "User Response Time Distribution" (.chart-card, 全宽)
    │   ├─ generateResponseTimeHistogram() — 靛蓝色 #6366f1
    │   │   桶: 2-10s | 10-30s | 30s-1m | 1-2m | 2-5m | 5-15m | >15m
    │   └─ "Median: {N}s · Average: {N}s"
    │
    ├─ "Multi-Clauding (Parallel Sessions)" (.chart-card, 全宽)
    │   ├─ 若无重叠: "No parallel session usage detected."
    │   └─ 若有重叠: Overlap Events | Sessions Involved | % of Messages (紫色大字)
    │
    ├─ Charts Row 3
    │   ├─ "User Messages by Time of Day" + <select> 时区选择器
    │   │   选项: PT(UTC-8) | ET(UTC-5) | London(UTC) | CET(UTC+1) | Tokyo(UTC+9) | Custom
    │   │   generateTimeOfDayChart() — 紫色 #8b5cf6
    │   │   时段: Morning(6-12) | Afternoon(12-18) | Evening(18-24) | Night(0-6)
    │   └─ "Tool Errors Encountered" — generateBarChart(tool_error_categories, '#dc2626') 红色
    │
    ├─ <h2 #section-wins> "Impressive Things You Did"
    │   ├─ .section-intro (intro 文本)
    │   └─ 3 个 .big-win 卡片（绿色背景 #f0fdf4）
    │       ├─ .big-win-title
    │       └─ .big-win-desc
    │
    ├─ Charts Row 4
    │   ├─ "What Helped Most (Claude's Capabilities)" — generateBarChart(success, '#16a34a') 绿色
    │   └─ "Outcomes" — generateBarChart(outcomes, '#8b5cf6', 固定顺序: not→partial→mostly→fully→unclear)
    │
    ├─ <h2 #section-friction> "Where Things Go Wrong"
    │   ├─ .section-intro (intro 文本)
    │   └─ 3 个 .friction-category 卡片（红色背景 #fef2f2）
    │       ├─ .friction-title
    │       ├─ .friction-desc
    │       └─ <ul> .friction-examples
    │
    ├─ Charts Row 5
    │   ├─ "Primary Friction Types" — generateBarChart(friction, '#dc2626') 红色
    │   └─ "Inferred Satisfaction (model-estimated)" — generateBarChart(satisfaction, '#eab308', 固定顺序: frustrated→dissatisfied→likely_satisfied→satisfied→happy→unsure)
    │
    ├─ <h2 #section-features> "Existing CC Features to Try"
    │   ├─ "Suggested CLAUDE.md Additions" (.claude-md-section, 蓝色背景)
    │   │   ├─ [Copy All Checked] 按钮
    │   │   └─ N 个条目，每个包含:
    │   │       ├─ <input type="checkbox" checked> + <code> addition 文本 + [Copy]
    │   │       └─ .cmd-why (why 说明)
    │   └─ Feature Cards (.feature-card, 绿色背景)
    │       ├─ .feature-title
    │       ├─ .feature-oneliner
    │       ├─ .feature-why
    │       └─ .example-code + [Copy] 按钮
    │
    ├─ <h2 #section-patterns> "New Ways to Use Claude Code"
    │   └─ Pattern Cards (.pattern-card, 蓝色背景)
    │       ├─ .pattern-title
    │       ├─ .pattern-summary
    │       ├─ .pattern-detail
    │       └─ .copyable-prompt + [Copy] 按钮
    │
    ├─ <h2 #section-horizon> "On the Horizon"
    │   ├─ .section-intro (intro 文本)
    │   └─ 3 个 .horizon-card（紫色渐变背景）
    │       ├─ .horizon-title
    │       ├─ .horizon-possible
    │       ├─ .horizon-tip ("Getting started: ...")
    │       └─ copyable prompt + [Copy]
    │
    ├─ Fun Ending (.fun-ending, 金色渐变)
    │   ├─ .fun-headline (引号包裹)
    │   └─ .fun-detail
    │
    └─ [ant-only] <h2 #section-feedback> "Closing the Loop: Feedback for Other Teams"
        ├─ .feedback-intro
        ├─ ▶ "Product Improvements for CC Team" (可折叠 .collapsible-section)
        │   └─ .feedback-card.team-card (蓝色背景)
        │       ├─ .feedback-title
        │       ├─ .feedback-detail
        │       └─ .feedback-evidence
        └─ ▶ "Model Behavior Improvements" (可折叠)
            └─ .feedback-card.model-card (紫色背景)

  </div>
  <script>{JavaScript — 约 100 行}</script>
</body>
</html>
```

### 5.2 CSS 设计系统

| 组件 | 背景色 | 边框色 | 文字色 |
|---|---|---|---|
| At a Glance | `linear-gradient(135deg, #fef3c7, #fde68a)` | `#f59e0b` | `#78350f` / `#92400e` |
| Big Win 卡片 | `#f0fdf4` | `#bbf7d0` | `#166534` / `#15803d` |
| Friction 卡片 | `#fef2f2` | `#fca5a5` | `#991b1b` / `#7f1d1d` |
| CLAUDE.md 区域 | `#eff6ff` | `#bfdbfe` | `#1e40af` |
| Feature 卡片 | `#f0fdf4` | `#86efac` | `#0f172a` |
| Pattern 卡片 | `#f0f9ff` | `#7dd3fc` | `#0f172a` |
| Horizon 卡片 | `linear-gradient(135deg, #faf5ff, #f5f3ff)` | `#c4b5fd` | `#5b21b6` |
| Fun Ending | `linear-gradient(135deg, #fef3c7, #fde68a)` | `#fbbf24` | `#78350f` |
| Team Card | `#eff6ff` | `#bfdbfe` | — |
| Model Card | `#faf5ff` | `#e9d5ff` | — |
| Key Insight | `#f0fdf4` | `#bbf7d0` | `#166534` |

### 5.3 柱状图渲染（`generateBarChart`）

```typescript
function generateBarChart(
  data: Record<string, number>,
  color: string,
  maxItems = 6,
  fixedOrder?: string[],
): string
```

- 每行: `.bar-label`(100px) + `.bar-track` > `.bar-fill`(百分比宽度) + `.bar-value`
- 固定顺序模式: Satisfaction 和 Outcomes 使用预定义顺序
- 标签通过 `LABEL_MAP` 映射为可读名称

### 5.4 JavaScript 交互功能

```javascript
// 折叠面板切换
function toggleCollapsible(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('open');
}

// 通用复制按钮（复制相邻 code 元素文本）
function copyText(btn) {
  const code = btn.previousElementSibling;
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
  });
}

// CLAUDE.md 单项复制（从 checkbox data-text 属性）
function copyCmdItem(idx) { ... }

// CLAUDE.md 批量复制所有已勾选项
function copyAllCheckedClaudeMd() { ... }

// 时区切换更新 Time of Day 图表
// rawHourCounts 内嵌为 JSON，offsetFromPT 调整小时偏移
function updateHourHistogram(offsetFromPT) { ... }
```

---

## 6. 导出格式（`InsightsExport` 类型）

除 HTML 外，还构建结构化 JSON 导出（用于 claudescope 消费和 S3 上传）：

```typescript
type InsightsExport = {
  metadata: {
    username: string          // process.env.SAFEUSER || USER
    generated_at: string      // ISO timestamp
    claude_code_version: string
    date_range: { start: string; end: string }
    session_count: number
    remote_hosts_collected?: string[]
  }
  aggregated_data: AggregatedData
  insights: InsightResults
  facets_summary?: {
    total: number
    goal_categories: Record<string, number>
    outcomes: Record<string, number>
    satisfaction: Record<string, number>
    friction: Record<string, number>
  }
}
```

---

## 7. LLM 调用总结

| 阶段 | 提示词 | 模型 | 执行方式 | maxTokens | 最大调用次数 |
|---|---|---|---|---|---|
| Facet 提取 | FACET_EXTRACTION_PROMPT + JSON schema | Opus | 并发 50 | 4096 | 50（未缓存 session） |
| 长转录摘要 | SUMMARIZE_CHUNK_PROMPT | Opus | per-session 并行 | 500 | 按需（>30k字符的session） |
| project_areas | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| interaction_style | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| what_works | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| friction_analysis | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| suggestions | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| on_the_horizon | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| cc_team_improvements | Section prompt | Opus | 并行（ant-only） | 8192 | 0-1 |
| model_behavior_improvements | Section prompt | Opus | 并行（ant-only） | 8192 | 0-1 |
| fun_ending | Section prompt | Opus | 与其他 section 并行 | 8192 | 1 |
| **at_a_glance** | 综合提示词 | Opus | **串行**（等前面完成） | 8192 | 1 |

**单次 `/insights` 执行总计**: 最多约 **50(facets) + N(摘要) + 7~9(sections) + 1(at_a_glance) = ~60** 次 Opus API 调用。

---

## 8. 关键设计特点

1. **三级缓存**: SessionMeta 缓存 + Facets 缓存 + 报告文件缓存，避免重复解析和 API 调用
2. **并行架构**: 6-9 个 insight section 并行生成，facets 50 并发提取，摘要按 session 内部并行
3. **渐进加载**: 文件系统先 stat → 选择性解析 JSONL → 选择性 API 提取
4. **Multi-clauding 检测**: 滑动窗口算法（30 分钟窗口）检测并发会话使用模式
5. **长转录处理**: >30k 字符自动分块摘要后再送入 facet 提取
6. **会话去重**: 同一 session_id 的多个分支保留用户消息最多的那个
7. **Meta-session 过滤**: 自动排除 `/insights` 自身生成的 API 调用日志
8. **懒加载**: 113KB 模块仅在 `/insights` 实际调用时才 import
9. **S3 上传**: Anthropic 内部用户自动上传到 S3 并返回可分享 URL
10. **facets 验证**: `isValidSessionFacets()` 校验必需字段，损坏缓存自动删除重建
