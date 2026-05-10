# OpenCortex 新链路功能对齐文档

本文档用于明确：旧 `opencortex` 记忆链路下线前，哪些产品功能已经由
`opencortex` 新链路覆盖，哪些功能需要在新链路重新实现，哪些旧能力可以
删除。

本文只按产品能力梳理，不按旧目录或旧类名继承设计。目标是让新链路成为唯一
运行时实现，旧链路不再作为产品记忆功能的依赖。

## 目标边界

新链路应该拥有以下边界：

- `core`：身份注入、中间件、请求上下文。
- `storage`：CFS 本地文件树、CortexStorage、SQLite 持久队列。
- `store`：memory/resource/session 写入流程、事件、writer。
- `vector`：Qdrant 写入、检索计划、执行、排序、CFS hydrate。
- `llm` 和 `prompts`：写入侧语义派生、ReasonTree 构建、召回侧 LLM 选择。

旧链路中的功能如果仍然是产品功能，应该按这些边界重新实现；不应继续保留旧
facade、旧 service、旧 ContextManager 作为运行时依赖。

## API 能力

新链路已经实现：

- `POST /api/v1/memory/store`
  - 写入 `memory` 或 `resource`。
  - `resource` 支持文档解析并写成 tree child records。
- `POST /api/v1/memory/search`
  - 执行 probe、planner、executor、ReasonTree、Cone、rank、hydrate。
- `POST /api/v1/memory/forget`
  - 支持语义删除和显式 URI 删除。
- `POST /api/v1/session/message`
  - 写 immediate message，并触发异步增强。
- `POST /api/v1/session/end`
  - flush pending merge，写 final session record。

需要判断是否继续产品化的旧 API：

- `GET /api/v1/memory/list`
- `GET /api/v1/memory/index`
- `GET /api/v1/memory/stats`
- `GET /api/v1/memory/health`
- `GET /api/v1/content/abstract`
- `GET /api/v1/content/overview`
- `GET /api/v1/content/read`
- `POST /api/v1/intent/should_recall`
- admin health/debug/reembed 类接口

倾向删除，除非有明确当前产品需求：

- reward feedback
- memory decay
- promote-to-shared
- migration-only APIs
- 旧 benchmark/admin 辅助接口

## Memory 写入

新链路当前行为：

1. HTTP 层验证 `StoreRequest`。
2. `MemoryStore` 通过 `CortexNamespace` 生成 URI。
3. 主路径只生成 raw primary record：
   - `id`
   - `uri`
   - `parent_uri`
   - `tenant_id`
   - `user_id`
   - `project_id`
   - `context_type`
   - `category`
   - `content`
   - `meta`
4. `PrimaryRecordWriter` 写入 Qdrant primary record。
5. 发布 `memory_stored` 事件。
6. worker 异步执行：
   - `SemanticDeriveAction`
   - `SearchIndexAction`
   - `EntityIndexAction`
   - `CortexStorageAction`
   - `ReasonTreeIndexAction`
   - `ReasonTreeBuildAction`
   - `CheckUpdateAction`

重要约束：

- 主路径不写 LLM 派生字段。
- overview、abstract、entities、keywords、reason tree 都由 worker 补齐。
- 不允许截断用户原始 content。
- 不允许无 LLM 兜底生成语义层。

未完成能力：

- `CheckUpdateAction` 目前只是事件钩子，还没有真实 update 检测逻辑。

## Resource / Document Parser

新链路当前行为：

1. `ResourceStore` 接收 `type=resource`。
2. `source` 和 `metadata` 被规范化到内部 meta。
3. parser 格式选择顺序：
   - `source.format`
   - `source.content_type`
   - `metadata.source_format`
   - `metadata.content_type`
   - `source.path` 文件扩展名
   - 默认 `markdown`
4. 支持的 parser family：
   - markdown
   - text
   - PDF
   - Word
   - Excel
   - PowerPoint
   - EPUB
5. 先写 resource root primary record。
6. 如果 parser 输出结构化 chunks，则写 root 下面的 child primary records。
7. child records 走同一套事件链路，因此会进入：
   - semantic derive
   - CFS L0/L1/L2
   - search index
   - entity index
   - reason tree index
   - reason tree build

请求示例：

```json
{
  "type": "resource",
  "content": "# Title\n\nBody",
  "category": "semantic",
  "metadata": {},
  "source": {
    "kind": "document",
    "path": "/docs/guide.pdf",
    "title": "Guide",
    "format": "pdf"
  }
}
```

注意：

- 当前 HTTP 请求仍然以 `content` 传入内容，不负责上传二进制文件。
- PDF/Word/Excel/PPT/EPUB parser 的第三方依赖是可选依赖；请求对应格式时，
  如果环境缺包，会在解析时失败。
- 如果后续需要真正的文件上传，应新增独立 upload/parse 边界，不要把二进制
  上传逻辑塞进 `memory/store`。

## Session Message

新链路当前行为：

1. `session/message` 接收一个 turn。
2. 每条 message 写成 immediate primary record。
3. immediate record 同步完成 embedding，并立即参与召回。
4. immediate 的 abstract 是完整 message content。
5. message 进入按 `(collection, tenant_id, user_id, session_id)` 隔离的 buffer。
6. 达到 token budget 后，buffer freeze 出待 merge chunk。
7. worker 执行 `SessionMergeAction`，写 merged record。
8. `SessionCleanupAction` 在 merged record 存在后清理 immediate vector projection。

重要约束：

- immediate 必须可召回。
- merge 前不能丢 immediate。
- merge 触发后必须冻结当时 chunk，不能继续累计后续 message 到同一 chunk。

## Session End

新链路当前行为：

1. `session/end` 获取 session lock。
2. freeze 所有剩余 buffer。
3. 同步写完所有 pending merged records。
4. 读取该 session 的 merged primary records。
5. 拼接 merged content，写 final session record。
6. 如果 final content 是结构化 markdown/tree，则写 child records。
7. final record 发布事件，进入同样的 worker index 链路。

当前策略选择：

- 新链路不直接复刻旧 full-session recomposition 的复杂聚类。
- 当前采用更清晰的方式：merged records + final record + ReasonTree 增强索引。

后续如要恢复复杂 recomposition，应作为新链路的独立 writer/action 设计，而
不是恢复旧 ContextManager。

## CFS / CortexStorage

新链路已经实现：

- L0：`.abstract.md`
- L1：`.overview.md`
- L2：`content.md`
- machine payload：`.abstract.json`
- read/write
- read_file/write_file
- append
- mkdir
- rm
- mv
- stat
- ls
- tree
- glob
- grep
- temp URI
- relation table
- link/unlink
- read_batch

可能需要重新实现的旧能力：

- content read API：
  - `GET /api/v1/content/abstract`
  - `GET /api/v1/content/overview`
  - `GET /api/v1/content/read`
- package import/export，如果它仍然是产品功能。

倾向不迁移：

- 与旧 AGFS 命名、ovpack 兼容相关的历史包袱，除非有明确外部用户依赖。

## Vector / Qdrant

新链路已经实现的 vector record 类型：

- primary record
- search anchor record
- search fact record
- entity projection record
- reason-tree projection record
- LLM-enhanced reason-tree node record

新链路已经支持：

- dense vector
- sparse vector 字段
- Qdrant payload indexes
- identity filter
- context/category filter
- retrieval_surface filter
- source_uri/parent_uri/tree metadata filter
- URI tree 删除
- 独立 Qdrant server URL

生产约束：

- 生产环境应使用独立 Qdrant Server。
- embedded/local Qdrant 只适合本地开发和测试。

## Retrieval

新链路当前行为：

1. `RetrievalProbe`
   - 对 query 做 embedding。
   - 大 query 可走 LLM query decomposition。
2. `RetrievalPlanner`
   - 决定 query size。
   - 决定 depth。
   - 决定 surface limits。
   - 决定是否使用 ReasonTree。
   - 决定是否使用 Cone。
3. `RetrievalExecutor`
   - 根据 plan 查询 Qdrant。
4. `ReasonTreeRunner`
   - 使用 LLM 在 reason-tree candidates 中选择 URI。
5. `ConeExpander`
   - 基于 metadata、relations、tree 邻域扩展。
6. `RetrievalRanker`
   - 合并多路 hits。
   - 计算最终分数。
7. hydrate
   - L1/L2 从 CFS 读取。

可能需要补的旧能力：

- explain summary/detail。
- search debug admin endpoint。
- probe-only endpoint：`/api/v1/intent/should_recall`。
- 更细的召回 trace 输出。

## Forget / Mutation

新链路已经实现：

- query semantic forget
- explicit URI forget
- CFS subtree 删除
- Qdrant primary/projection 删除

尚未实现：

- update 检测。
- mutation record。
- consolidation policy。
- decay。
- reward feedback。

建议新链路后续实现顺序：

1. 将 `CheckUpdateAction` 做成真实 update detector。
2. 如需审计，新增 mutation event/record。
3. 如果确实需要，再实现 decay。
4. reward feedback 只有在当前排序策略要用时才恢复。

## Worker / Queue

新链路已经实现：

- SQLite 持久队列。
- enqueue。
- dequeue。
- ack。
- release。
- fail。
- stale recovery。
- failed requeue。
- 多 worker concurrency。
- session ordering key。

设计约束：

- 同一 session 的状态变更必须串行。
- 不同 session、不同 resource、不同 memory 可以并行。
- worker 失败必须进入可观察、可重试状态。

## 旧链路清理前决策表

必须保留并在新链路实现：

- resource 多格式 parser。
- memory/resource/session 写入。
- CFS L0/L1/L2。
- Qdrant primary/vector/projection。
- retrieval 主链路。
- semantic forget。
- durable worker queue。

建议保留，但要新实现：

- memory list/index/stats/health。
- content read endpoints。
- intent should_recall/probe-only endpoint。
- admin search debug。
- admin reembed。

按需保留：

- feedback。
- decay。
- promote-to-shared。
- package import/export。

建议删除：

- 旧 ContextManager 写入链路。
- 旧 CortexMemory 作为产品 facade 的记忆主链路。
- 旧 service wrapper 层。
- 旧 request_context 中只服务旧 HTTP 链路的概念。
- 旧 benchmark/admin 辅助逻辑。
- 旧 AGFS 命名和兼容层。

## 当前结论

新链路已经可以承接主产品闭环：

- 写入 memory。
- 写入 resource。
- 解析 document tree。
- 写入 session immediate。
- 写入 merged/session final。
- 写 CFS。
- 写 Qdrant。
- worker 异步增强。
- 多路召回。
- semantic forget。

旧链路可以开始下线，但应分阶段执行：先让旧 API 入口转发到新链路，再补
list/stats/content/probe/admin 这些仍需保留的产品能力，最后删除旧实现。
