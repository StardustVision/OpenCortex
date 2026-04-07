# 自噬式记忆流程（Autophagy）

## 为什么它存在

自噬式记忆流程（Autophagy）是一套“召回并记录”的生命周期机制，它让记忆能力不再只是无状态的搜索 API，而成为具备会话感知能力的协议。`memory_context` 流程负责协调规划、检索和会话记账，使系统能够：1）决定何时执行召回；2）返回上下文以及配套指引；3）通过带缓冲的对话写入和后续 `trace` 处理，将当前轮次持久化下来。

## 核心组件

- `ContextManager`：负责 `memory_context` 的 `prepare` / `commit` / `end` 生命周期，缓存准备阶段结果，跟踪每个会话的状态，并管理对话缓冲。
- `MemoryOrchestrator.plan_recall()`：通过 `IntentRouter` 与 `RecallPlanner` 判定召回意图并生成 `RecallPlan`。
- `RecallPlanner`：把 `SearchIntent` 转换为显式的 `RecallPlan`（包含要检索的召回对象、结果上限、细节层级、cone 标记）。
- `IntentRouter`：三层意图分析（关键词、可选 LLM、记忆触发器），产出 `SearchIntent` 与类型化查询。
- `RecallPlan` / `RecallSurface`：`RecallPlan` 是完整的召回计划；`RecallSurface` 则是其中用来标记“要检索哪些召回对象”（例如记忆或知识）的枚举维度。
- `Observer` + Alpha 管线：`ContextManager._commit()` 负责记录对话转录，`ContextManager._end()` 则委托 `orchestrator.session_end()` 执行 `trace` 拆分、存储，以及可选的知识候选生成。

## 准备阶段

1. 调用 `POST /api/v1/context`，并携带 `phase="prepare"`，请求会分发到 `ContextManager._prepare()`。
2. 该调用会按 `(tenant, user, session, turn)` 通过准备阶段缓存保证幂等；首次调用会创建 `Observer` 会话。
3. 最新一条用户消息会被视为查询。如果没有用户查询，`prepare` 会返回一个空结果，并设置 `should_recall=false`。
4. 除非 `recall_mode="never"`，否则会调用 `MemoryOrchestrator.plan_recall()`：先由 `IntentRouter` 生成 `SearchIntent`，再由 `RecallPlanner` 产出 `RecallPlan`。这一步带有超时控制，失败时会回退到本地召回方案。
5. 检索会依据该方案并行执行：
   - 记忆检索通过 `orchestrator.search()` 执行，使用方案中的细节层级、结果上限，以及可选的 `context_type` / `category` 过滤条件。
   - 当启用了 `include_knowledge` 且方案允许知识召回时，通过 `orchestrator.knowledge_search()` 执行知识检索。
6. 响应会打包 `intent`（其中包含 `intent.recall_plan`）、记忆结果、知识结果，以及引用指引（基于置信度的引用提示）。若走空 `prepare` 兜底，则响应结构会缩减，不包含 `recall_plan`。

## 提交流程

1. `phase="commit"` 会校验当前轮次至少包含两条消息，并按 `turn_id` 保证幂等。
2. `Observer` 会记录完整轮次（包括 `tool_calls`）。如果失败，则会写入一条兜底的 JSONL 记录。
3. 被引用的 URI 会异步更新奖励。只有在启用了 `SkillEventStore`，且服务端在 `prepare` 阶段确实跟踪到了被选中的技能 URI 时，才会执行技能引用校验（`skill citation validation`）。
4. 对话缓冲：
   - 每条消息都会先通过 `_write_immediate()` 立即写入，以便快速召回（`meta.layer="immediate"`）。
   - 消息与工具调用会被追加到各自会话的缓冲区中。
   - 当缓冲超过 token 阈值后，会通过 `orchestrator.add()` 将其合并为质量更高的分块，并删除对应的即时层记录。

## 结束流程

1. `phase="end"` 会把剩余的对话缓冲全部刷写为合并记录，并清理遗留的即时层记录。
2. `orchestrator.session_end()` 会运行 Alpha 管线（`Observer` flush、`trace` 拆分与存储）。是否异步触发 Archivist 工作，取决于配置与调用路径。当前这一路径返回的结果以 `trace` 计数为主；`knowledge_candidates` 在这里实际上会被报告为 `0`。
3. 最后会清理会话状态、缓存和轮次跟踪信息。空闲会话也会由后台清扫器按同样的结束流程自动关闭。

## 与搜索和知识召回的关系

自噬式记忆流程（Autophagy）不是对 `search()` 的一层薄包装。普通的 `search()` 虽然同样会使用 `plan_recall()`，但它不负责生命周期状态、`Observer` 记录或对话缓冲。`memory_context` 会结合会话上下文生成召回方案，并行执行记忆召回与可选的知识召回，然后把引用指引一并返回给调用方；之后再由 `commit` / `end` 完成记录与会话收敛。

知识召回是一类独立的召回对象，可由服务端配置和召回方案共同决定是否启用。`ContextManager` 在调用记忆检索时也可以施加内部过滤条件（例如 `context_type` 或 `category`），但这些并不是对外公开、受保证的 `/api/v1/context` 请求参数。知识召回本身有边界限制（`knowledge_limit` 会被封顶），并依赖 Alpha 知识存储，因此它是可选能力，即使关闭也不影响记忆召回。会话缓冲则保证新产生的对话轮次能立即被检索到，随后再延后合并为更高质量的召回材料。

## 约束与权衡

- 相比无状态搜索 API，它引入了有状态能力（会话缓存、幂等跟踪、缓冲区）。
- 召回规划是尽力而为的：当会话上下文或 LLM 不可用时，`IntentRouter` 会跳过 LLM 路径；`prepare` 在超时或失败时会回退到本地方案。
- 即时层写入让新轮次能被快速搜索到，但后续需要额外的合并与清理工作，也会让即时层与合并层之间出现最终一致性窗口。
- 这套协议把生命周期编排集中起来了，但也带来了更多活动部件（`Observer` 可用性、异步奖励任务、后台清理）。

## 当前状态

自噬式记忆流程（Autophagy）这一层行为已经通过 `ContextManager` 和 `/api/v1/context` 端点实现。召回规划是显式的（由 `RecallPlanner` 产出 `RecallPlan`），意图分类由 `IntentRouter` 驱动。当前生命周期已经覆盖记忆召回、可选知识召回与会话缓冲，但并不存在单独的 `Autophagy` 模块；它本质上是跨 `ContextManager`、`MemoryOrchestrator` 与检索组件的一层协调逻辑。`RecallSurface` 虽然定义了 `TRACE`，但当前 `prepare` 路径不会执行 `trace` 召回。
