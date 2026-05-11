# OpenCortex MCP 与 Agent Hooks 记忆接入设计

## 背景

OpenCortex 目前已经提供 Streamable HTTP MCP 服务，暴露以下工具：

- `opencortex.search`
- `opencortex.session_message`
- `opencortex.session_end`
- `opencortex.store_memory`
- `opencortex.store_resource`
- `opencortex.forget`

MCP 本身是被动工具协议。模型是否调用工具，取决于客户端、模型和 prompt 约束。为了提高默认调用率，本阶段只加强 MCP `instructions` 和 `opencortex-memory-rules` prompt，不实现 Codex、Claude Code、OpenCode 的 hooks/plugin。

后续如果要保证“每句话都记录”，必须在客户端生命周期层实现 hooks 或插件。原因是只有 hooks/plugin 能在模型调用工具之前或之后，由程序强制捕获用户输入、助手输出和会话结束事件。

## 本阶段 MCP Prompt 规则

MCP prompt 的规则按输入形态分流：

1. 普通对话 turn 走 `opencortex.session_message`。
2. 明确要求记忆、偏好、事实、决策、需求、重要结果走 `opencortex.store_memory`。
3. 长文档、资料、规范、API 文档、粘贴文章、可复用参考材料走 `opencortex.store_resource`。
4. 上下文相关回答前走 `opencortex.search`。
5. 会话结束走 `opencortex.session_end`。
6. 删除或遗忘只在用户明确要求时走 `opencortex.forget`。

关键原则：`store_memory` 不替代 `session_message`。如果一句话既是普通对话 turn，又包含明确要记忆的内容，可以同时写 session 和 memory。普通对话不应该每句直接写 long-term memory，避免召回噪音和记忆污染。

## 数据分层

### Session Message

用途：完整记录对话事实。

写入内容：

- 用户消息
- 助手消息
- 必要的工具调用摘要
- 用户明确引用的 URI
- turn id、session id、消息角色、时间等元信息

后续处理：

- immediate record 可参与召回
- merge 生成整理后的 session 内容
- session end 生成最终结构化 tree / summary
- worker 通过 LLM 提取长期 memory、reason tree、search indexes

### Store Memory

用途：明确的长期记忆。

触发条件：

- 用户说“记住”
- 用户表达偏好、习惯、约束、身份信息
- 用户做出决策
- 项目中形成稳定事实
- 用户强调“重要”
- 需要立即参与后续召回的事实

不适合：

- 普通寒暄
- 临时问题
- 大段文档
- 一次性中间推理

### Store Resource

用途：可复用的大段资料。

触发条件：

- 用户要求录入文档
- 用户粘贴规范、API 文档、设计文档、会议纪要、需求说明
- 用户提供 URL、文件、长文本材料
- 内容主要是外部知识或项目资料，而不是个人记忆

后续处理：

- 文档解析
- CFS 写入
- primary index
- search index
- reason tree index

## 为什么只靠 MCP 不够

MCP `instructions` 和 prompt rules 可以提升调用率，但不能保证完整记录：

- 模型可能忘记调用工具。
- 部分客户端不会把 MCP instructions 注入模型上下文。
- 部分客户端不会自动加载 MCP prompts。
- 工具调用失败后，客户端未必会重试。
- assistant 输出后的记录需要客户端生命周期事件，普通 MCP 无法强制执行。

因此 MCP prompt 适合“通用客户端兼容”，hooks/plugin 才适合“完整记录保证”。

## 后续 Hooks 设计目标

目标：

- 每个有效 user / assistant turn 都进入 `opencortex.session_message`。
- 明确 durable memory 额外进入 `opencortex.store_memory`。
- 长文档进入 `opencortex.store_resource`。
- 对话前自动 recall。
- 会话结束自动 `opencortex.session_end`。
- 不把 OpenCortex 注入的 recall context 再写回 OpenCortex。
- 写入失败可观测、可重试、不中断用户对话。

非目标：

- 不在 hook 中直接做复杂 LLM 抽取。
- 不让 hooks 决定所有长期 memory 内容。
- 不把每句普通对话都直接写成 long-term memory。

## Claude Code Hooks 设计

Claude Code 有成熟 lifecycle hooks，适合作为优先实现对象。

### 事件映射

| Claude Code Hook | OpenCortex 行为 |
|---|---|
| `SessionStart` | 建立或恢复 OpenCortex session，必要时注入最近 session overview |
| `UserPromptSubmit` | 调 `opencortex.search`，把结果作为私有上下文注入 prompt |
| `Stop` | 解析 transcript，提取新增 user/assistant turns，调用 `opencortex.session_message` |
| `PreCompact` | compact 前 flush 当前 session，避免上下文被客户端改写后丢失 |
| `SessionEnd` | 调 `opencortex.session_end`，生成最终 session tree |
| `SubagentStart` | 为 subagent 派生独立 session id |
| `SubagentStop` | 读取 subagent transcript，写入独立 session 并结束或提交 |

### 记录策略

`Stop` hook 读取 transcript，而不是只读最后一句。这样可以避免流式输出、工具输出、assistant 多段消息造成漏记。

每次处理后保存本地 offset：

- `captured_turn_count`
- `last_message_id`
- `session_id`
- `last_success_at`

下一次只处理新增 turn。即使 hook 重复触发，也不会重复写入。

### 过滤策略

写入 session 前必须剥离：

- `<opencortex-context>...</opencortex-context>`
- `<openviking-context>...</openviking-context>`
- `<relevant-memories>...</relevant-memories>`
- system reminder
- subagent context header

目的：避免本轮 recall 结果在下一轮被当成用户输入再次写回，形成自我污染。

### 失败处理

`Stop`、`SessionEnd`、`SubagentStop` 应采用 detached worker：

1. 父 hook 读取 stdin。
2. 立即返回 approve，避免阻塞 Claude Code。
3. 子进程异步执行 HTTP 写入。
4. 失败记录到本地 dead-letter 文件。
5. 下一次 hook 或 session start 尝试重放。

`PreCompact` 需要同步执行，因为客户端会马上改写 transcript。

## Codex Hooks 设计

Codex CLI 当前已有 hooks 和 plugin 能力。Codex 的设计应避免只做 MCP-only `remember`，因为那只适合显式记忆，不适合完整会话记录。

### 事件映射

| Codex Hook | OpenCortex 行为 |
|---|---|
| `SessionStart` | 建立 session 映射，恢复未完成写入 |
| `UserPromptSubmit` | 调 `opencortex.search` 并通过 `additionalContext` 注入 recall |
| `Stop` | 捕获 assistant 输出，读取 transcript 时优先全量增量解析，调用 `opencortex.session_message` |
| `PreToolUse` | 可选：记录关键工具调用意图摘要 |
| `PostToolUse` | 可选：记录关键工具结果摘要 |

### 记录策略

Codex 需要一个稳定 session id：

- 优先使用客户端提供的 session id。
- 如果没有，使用工作目录、启动时间、进程 id、用户 id 生成本地 session id。
- session id 映射持久化在本地状态文件中。

每个 turn 生成稳定 turn id：

- 优先 message id。
- 否则使用 role、content hash、timestamp bucket。

### Memory 与 Resource 分流

Codex prompt 或 hook worker 可以使用轻量规则识别：

- `remember`、`记住`、`偏好`、`决定`、`important`、`always`、`never` 等显式记忆词，额外调用 `store_memory`。
- 大于阈值的粘贴文本、Markdown 文档、代码/配置/API 文档，额外调用 `store_resource`。
- 所有有效对话仍然调用 `session_message`。

长期记忆是否最终成立，以服务端 LLM merge/extraction 为准。

## OpenCode Plugin 设计

OpenCode 插件系统适合做完整接入，因为它支持事件监听和消息 transform。

### 事件映射

| OpenCode 能力 | OpenCortex 行为 |
|---|---|
| `session.created` | 创建 OpenCortex session 映射 |
| `message.updated` | 记录 role、完成状态、token/cost 元信息 |
| `message.part.updated` | 捕获流式 text part，合并为完整消息 |
| `session.deleted` / `stop` | flush pending messages，调用 `session_end` |
| `experimental.chat.messages.transform` | 在最新 user message 后追加 recall context |

### 记录策略

OpenCode 应以 message id 为去重主键：

- `pending_messages`: message id 到内容片段
- `message_roles`: message id 到 user/assistant
- `captured_messages`: 已写入 OpenCortex 的 message id 集合
- `sending_messages`: 正在写入的 message id 集合

只有当 role 和内容都完整时，才调用 `session_message`。assistant 消息应等待 finish/stop 后写入，避免记录半截输出。

### 自动提交

OpenCode 插件可启动轻量 scheduler：

- 定期 flush pending messages。
- 达到 token/turn 阈值后触发后台 commit 或 session merge。
- session 删除时强制 flush 并 `session_end`。

## 识别规则

Hooks/plugin 不应该做复杂语义抽取，但可以做轻量分类以决定是否额外调用 `store_memory` 或 `store_resource`。

### 明确记忆词

英文：

- remember
- preference
- prefer
- important
- decision
- decided
- always
- never
- favorite

中文：

- 记住
- 偏好
- 喜欢
- 重要
- 决定
- 总是
- 永远
- 优先
- 习惯
- 爱好
- 擅长
- 最爱
- 不喜欢

### Resource 识别

满足任一条件时倾向 `store_resource`：

- 内容超过普通对话阈值。
- Markdown 标题密集。
- 包含目录、章节、表格、代码块。
- 用户说“录入文档”、“保存资料”、“这是规范/API/设计文档”。
- 内容来自文件、URL、粘贴的长文档。

### 过滤项

以下内容不额外写 `store_memory`：

- 纯问题
- 纯命令
- 纯标点
- 太短内容
- 工具日志噪音
- 已注入的 recall context

但如果它是有效对话 turn，仍可进入 `session_message`。

## 完整性保障

后续 hooks/plugin 必须具备以下机制：

1. 增量 offset：避免漏记和重复写。
2. 本地持久状态：session map、pending messages、dead letter。
3. 幂等 turn id：同一 turn 重放不会写出多份。
4. 异步写入：不阻塞用户主交互。
5. 失败重试：网络失败、401、5xx、超时都要可观测。
6. 污染防护：剥离 recall 注入块。
7. 明确分流：session/message、memory、resource 各走各的工具。

## 推荐实现顺序

1. Codex hooks/plugin：优先服务当前使用场景，支持完整 session_message 和自动 recall。
2. Claude Code hooks/plugin：对齐成熟 lifecycle，覆盖 session start/stop/compact/subagent。
3. OpenCode plugin：使用 event + transform，完整记录 message stream。

每个实现都应先只保证：

- 对话完整进入 session。
- recall 自动注入。
- session end 完整执行。

再逐步增加：

- 显式 memory 额外写入。
- resource 自动识别。
- dead-letter replay。
- 状态面板和调试日志。
