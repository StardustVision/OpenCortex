# Supermemory 调查报告

日期：2026-04-03

## 1. 调查范围与结论

本次调查基于仓库 `/Users/hugo/CodeSpace/Work/supermemory` 的静态探索完成，重点覆盖以下问题：

- 这个仓库的真实定位是什么
- 架构、设计、功能实现分别落在哪里
- 后端“记忆体系”在代码中可见到什么程度
- 是否存在“自学习”能力
- 这些发现对 OpenCortex 是否有吸收价值

最重要的结论先说清楚：

1. `supermemory` 这个仓库不是完整的业务后端仓库，而是一个以产品前端、MCP 适配层、共享 SDK、共享 schema、图谱引擎为核心的 monorepo。
2. 真正的记忆引擎服务是存在的，但其核心实现不在本仓库中，而是在远端 `api.supermemory.ai`。
3. 仓库中可以清晰看到“记忆系统接口面”和“记忆系统消费层”，包括：写入、画像、召回、prompt 注入、MCP 暴露、图谱展示。
4. 从接口和 schema 可以反推出其后端记忆模型并不浅层，至少支持：增量更新、画像压缩、混合检索、记忆关系、版本链、遗忘语义、推断型记忆。
5. 对 OpenCortex 来说，这份调查最有价值的是架构方法论、数据模型和产品化路径，而不是底层引擎算法源码复用。

## 2. 仓库定位

### 2.1 工作区形态

根目录是标准 monorepo：

- `apps/*`
- `packages/*`
- `bun + turbo`

可见入口：

- `package.json`
- `turbo.json`

从工作区结构看，这个仓库主要由三层组成：

1. 产品层
   - `apps/web`
   - `apps/mcp`
2. 共享能力层
   - `packages/lib`
   - `packages/validation`
   - `packages/memory-graph`
   - `packages/tools`
   - `packages/ui`
3. 生态与 SDK 层
   - 多个 Python SDK
   - 浏览器扩展
   - Raycast 扩展

### 2.2 不是完整后端仓库

这点非常关键。

多个地方都直接指向远端 API：

- `packages/lib/api.ts` 的 `$fetch` 指向 `${NEXT_PUBLIC_BACKEND_URL}/v3`
- `packages/tools/src/shared/memory-client.ts` 直接请求 `https://api.supermemory.ai/v4/profile`
- `packages/tools/src/conversations-client.ts` 直接请求 `https://api.supermemory.ai/v4/conversations`
- `apps/mcp/src/client.ts` 通过 SDK/HTTP 访问远端 Supermemory API

因此这个仓库中真正存在的是：

- 前端应用
- MCP server 协议壳
- SDK
- typed fetch 层
- schema
- graph UI / graph engine

而不在这个仓库中的，是“抽取记忆、维护画像、检索排序、重写 query、自动遗忘”等真正的后端引擎实现。

## 3. 核心架构分层

## 3.1 Schema 与 API 访问层

共享 schema 定义在 `packages/validation/api.ts` 与 `packages/validation/schemas.ts`。

这一层承担两件事：

- 统一对外 API 输入输出类型
- 暴露底层记忆对象、搜索对象、文档对象的结构

统一请求封装在 `packages/lib/api.ts`，通过 typed fetch 连接远端 `/v3` API。

这是整仓最清晰、最稳的基础层之一。对于前端、MCP、SDK 来说，它相当于“协议真源”。

## 3.2 Web 产品层

消费者应用主入口是 `apps/web`。

`apps/web/app/layout.tsx` 说明其运行模型是：

- `AuthProvider` 负责认证上下文
- `QueryProvider` 负责 React Query
- `NuqsAdapter` 负责 URL 状态
- `PostHogProvider` 等负责监控和分析

`apps/web/app/(app)/page.tsx` 是整套消费者产品的 orchestrator。它通过 URL 参数控制：

- add document modal
- search modal
- document modal
- chat sidebar
- fullscreen note
- graph / list / integrations 视图

也就是说，消费者产品不是多页面强分治，而是一个“大单页 + 多模式切换”的应用壳。

## 3.3 MCP 协议层

`apps/mcp` 是 Cloudflare Worker + Durable Object 形式的 MCP server。

这里的职责不是实现记忆引擎，而是把远端能力包装成 MCP 可调用协议：

- `memory`
- `recall`
- `context`
- `whoAmI`
- `listProjects`
- graph app

换句话说，`apps/mcp` 是“协议接入层”，不是“知识引擎层”。

## 3.4 记忆图引擎层

`packages/memory-graph` 是独立的图谱渲染与交互引擎。

它不是普通 React 图组件，而是：

- React 只做编排
- 画布、viewport、simulation、hit-test 全在 imperative runtime 中完成
- canvas + rAF + refs 驱动热路径

这块是仓库里工程含量最高的本地实现之一。

## 4. Web 侧实现链路

## 4.1 文档与内容写入

用户在 web 侧添加内容，最终会走到 `apps/web/hooks/use-document-mutations.ts`。

这里做了几件很重要的事：

- 调用 `@post/documents`
- 传 `containerTags`
- 传 `entityContext`
- 做 optimistic update
- 失败时回滚 React Query 缓存

`entityContext` 的实际用途很值得注意。前端在保存笔记和上传文件时，不只是传“原始文本”，还会传关于“这是谁、这是什么类型知识”的解释性上下文。这说明后端抽取 pipeline 很可能会利用这一段上下文提高记忆抽取质量。

## 4.2 搜索与命令面板

搜索入口一部分在命令面板 `apps/web/components/documents-command-palette.tsx`，另一部分在聊天和文档流中。

命令面板设计很直接：

- 无搜索词时读缓存文档
- 有搜索词时打 `@post/search`
- 支持容器标签过滤

这里有一个潜在实现风险：命令面板读取缓存时使用的 query key 与主文档流不完全一致，存在 cache miss 或错误预期的可能。

## 4.3 Chat / Nova

聊天侧栏在 `apps/web/components/chat/index.tsx`。

关键特点：

- 使用 `DefaultChatTransport`
- 直连 `${NEXT_PUBLIC_BACKEND_URL}/chat`
- metadata 中带 `chatId`、`projectId`、`model`
- thread 历史通过 `/chat/threads` 接口维护

assistant 消息不是字符串拼接，而是结构化 `parts` 渲染，见 `apps/web/components/chat/message/agent-message.tsx`。其中会展示：

- 文本部分
- tool call
- web sources
- related memories

这说明 chat 产品不是简单的 RAG UI，而是 agent UI。

## 4.4 Graph 视图

graph 视图壳在 `apps/web/components/graph-layout-view.tsx`，数据适配在 `apps/web/components/memory-graph/hooks/use-graph-api.ts`。

这一层负责把后端文档对象：

- `type`
- `memoryEntries`

转换为图组件需要的：

- `documentType`
- `memories`

这是一个非常重要的适配层，因为后端 payload 结构和图引擎输入模型并不相同。

## 5. MCP 侧实现链路

## 5.1 鉴权与会话

`apps/mcp/src/index.ts` 是请求入口。

其工作是：

- 校验 Bearer Token
- 识别 `x-sm-project`
- 把 userId、apiKey、containerTag 等信息注入上下文

`apps/mcp/src/auth.ts` 负责两类 token：

- `sm_` 前缀的 API key
- OAuth token

OAuth token 最终会被换成下游 API key，因此 MCP 对外看起来是 OAuth，会话内部则转为 API key 调用远端服务。

## 5.2 工具处理

真正的 MCP tool handler 在 `apps/mcp/src/server.ts`。

最核心的几个方法：

- `handleMemory()`
- `handleRecall()`
- `whoAmI`
- `listProjects`
- `context`

而这些 handler 又通过 `apps/mcp/src/client.ts` 调远端 Supermemory SDK 或原始 API。

这里可以明确看到能力映射：

- `createMemory()` -> `client.add(...)`
- `search()` -> `client.search.memories(...)`
- `getProfile()` -> `client.profile(...)`
- `forgetMemory()` -> `client.memories.forget(...)`

因此 MCP 本质是：

`MCP 客户端 -> 协议层 -> 远端记忆引擎`

## 5.3 MCP graph app 风险

`apps/mcp` 中嵌入了一个 graph UI，但它没有复用 `packages/memory-graph` 和 web 里的适配逻辑，而是自己做了一套 transform。

这导致了几个严重风险：

- payload 结构漂移
- 图关系逻辑重复
- UI 与共享图模型不一致
- 大数据分页能力未真正打通

这是当前架构里最明显的维护债之一。

## 6. 记忆体系：能看到什么

## 6.1 可见主链

从仓库可见证据，可以还原出后端记忆体系的主链：

1. 写入原始内容
2. 后端抽取结构化记忆
3. 把记忆压缩成用户/项目画像
4. 针对 query 做 recall / hybrid search
5. 注入到 prompt 或通过 MCP / tools 暴露给模型

注意：步骤 2-4 的内部算法并不在仓库里，但接口和 schema 可以把这条链基本还原出来。

## 6.2 写入不是直接写 facts，而是写 raw content

文档 `apps/docs/add-memories.mdx` 说得很明确：

- 输入可以是文本、对话、URL、文件、HTML
- 系统自动提取 memories
- `customId` 用来做更新和去重
- 同一内容源可以增量上送

同时 `packages/tools/src/conversations-client.ts` 提供了 `/v4/conversations` 结构化写入能力，支持：

- message roles
- multimodal content
- tool calls
- append detection / smart diffing

这说明后端摄取层比“把一段字符串入库”要成熟得多。

## 6.3 记忆对象不是扁平结构

`packages/validation/schemas.ts` 中的 `MemoryEntrySchema` 暴露出几个关键字段：

- `version`
- `isLatest`
- `parentMemoryId`
- `rootMemoryId`
- `memoryRelations`
- `isInference`
- `isForgotten`
- `forgetAfter`
- `forgetReason`

这意味着后端记忆对象至少被设计成：

- 可演化
- 可追溯
- 有关系
- 有生命周期
- 可区分直接事实和推断事实

这和普通的“embedding + chunk”系统已经不是一个层级。

## 6.4 Profile 是压缩层，不是简单列表

画像接口是 `/v4/profile`，见：

- `apps/docs/user-profiles.mdx`
- `apps/docs/user-profiles/api.mdx`
- `packages/tools/src/shared/memory-client.ts`

返回结构固定是：

- `profile.static`
- `profile.dynamic`
- 可选 `searchResults`

文档把它定义为“real-time dynamic compaction”。这意味着 profile 层本质上是在做“记忆压缩”：

- 长期稳定事实 -> static
- 近期活动 / 当前状态 -> dynamic

这一步对 prompt 质量极其关键，因为它避免了将全部原始记忆直接拼接给模型。

## 6.5 Search 是 recall 层

召回能力主要通过 `/v4/search` 和 `/v4/profile(q=...)` 暴露。

`apps/docs/search.mdx` 能看到：

- `searchMode: "hybrid"` 为推荐模式
- hybrid 同时搜索 extracted memories 和 document chunks
- `threshold` 控制相似度门槛
- `rerank` 做二次重排
- 支持 metadata filters

changelog 中还提到：

- `query rewriting`
- `documentThreshold`
- `chunkThreshold`
- `includeFullDocs`
- `includeSummary`

这说明搜索质量优化不仅是向量召回，还涉及 query 处理、重排和多阶段筛选。

## 7. 系统是否有“自学习”

这里要严格区分两种含义。

### 7.1 没有证据表明存在权重级在线训练

从本仓库看不到任何能证明“LLM 权重会在线更新、持续微调”的实现。

所以如果“自学习”指的是模型参数级自训练，那么本仓库无法证明。

### 7.2 明确存在记忆级自学习 / 上下文级自学习

如果“自学习”指的是：

- 自动从对话和文档中沉淀信息
- 自动维护 profile
- 自动影响下一轮召回和 prompt

那么答案是明确有。

证据包括：

- README 直接写了 automatically learns from conversations
- `addMemory: "always"` 的 middleware 会自动保存对话
- `/v4/profile` 自动维护 static/dynamic profile
- recall 与 prompt injection 都会在后续轮次消费这些结果

因此更准确的说法是：

- 有记忆级自学习
- 无法从仓库证明有权重级自学习

## 8. 它如何优化记忆质量

从仓库可见证据看，它优化记忆质量不是靠单一技巧，而是全链路控质。

### 8.1 摄取质量

- `customId` 避免重复 ingest，支持 diff / append
- `entityContext` 为抽取提供解释上下文
- 自动识别多种内容类型（PDF、图片、视频、网页）

### 8.2 结构质量

- 用 `version`、`parentMemoryId` 管理知识更新
- 用 `memoryRelations` 建关系
- 用 `isInference` 区分推断
- 用 `isForgotten`、`forgetAfter` 管理生命周期

### 8.3 画像质量

- 将大量记忆压缩成 `static` / `dynamic`
- 降低 prompt 噪声
- 提高长期偏好与近期上下文的区分度

### 8.4 召回质量

- `hybrid` 搜索同时检索 memory 与 chunk
- `threshold` 控制召回精度
- `rerank` 提升 top 结果排序质量
- `query rewriting` 改善查询表达
- `documentThreshold` / `chunkThreshold` 细化召回控制

### 8.5 注入质量

`packages/tools/src/tools-shared.ts` 中对 memory 注入前做了显式去重，优先级是：

- `static`
- `dynamic`
- `searchResults`

这能减少同一事实多次出现在 prompt 中。

## 9. memory-graph 的价值

`packages/memory-graph` 是这个仓库中最值得单独评价的本地实现。

它的设计特点：

- React 负责 orchestration
- canvas/rAF/ref 负责热路径
- 有 viewport 模型、force simulation、spatial index、version-chain index
- 支持图层级的可视化调试

这并不是后端引擎本体，但它对“如何理解和调试记忆系统”非常重要。

对于 OpenCortex 来说，这部分的吸收价值主要在：

- observability
- 可视化调试
- 关系/版本/遗忘状态的呈现

## 10. 对 OpenCortex 的吸收价值

结论是：有，而且价值不低，但主要是架构与产品方法论，不是引擎源码复用。

### 10.1 建议吸收

1. 记忆系统分层
   - 摄取层
   - 结构化记忆层
   - profile 压缩层
   - recall 层
   - SDK / MCP 分发层

2. 数据模型设计
   - 版本链
   - 记忆关系
   - 遗忘语义
   - 推断 vs 直接来源

3. 质量控制方法
   - `customId`
   - `entityContext`
   - `static/dynamic`
   - `threshold/rerank/hybrid`
   - 注入前去重

4. agent 集成策略
   - middleware 自动注入
   - tool calling
   - MCP 暴露
   - consumer app 协同

### 10.2 谨慎吸收

1. `apps/web` 的“大单页 orchestrator”模式
   - 迭代快
   - 但状态耦合高

2. graph 的双实现问题
   - MCP 侧重复 graph 逻辑不是好范式

### 10.3 不应高估

1. 仓库中并没有核心记忆引擎源码
2. 看不到真正的抽取、压缩、索引、重排算法
3. 所以不应把本次调查误判为“已经拿到 Supermemory 的后端实现方法”

## 11. 最终判断

如果只看本仓库，Supermemory 最强的不是“后端源码开放”，而是它已经形成了一套完整且成熟的“记忆产品接口形态”：

- 原始内容摄取
- 结构化记忆
- 自动画像
- 混合召回
- prompt 注入
- MCP 暴露
- 图谱可视化

因此，它对 OpenCortex 的价值不在于源码照抄，而在于：

- 把记忆从“向量检索功能”提升为“完整上下文系统”
- 把记忆系统产品化、协议化、工具化
- 把质量控制分散到写入、压缩、召回、注入的全链路

## 12. 调查边界

本报告基于静态代码与文档探索完成，未包含：

- 远端 `api.supermemory.ai` 的私有服务源码
- 生产环境实际召回效果验证
- 在线接口实验结果
- benchmark 复现实验

因此以下判断属于“基于接口与 schema 的高可信反推”，而非源码级证实：

- profile 内部压缩算法
- query rewriting 实现细节
- reranking 具体模型
- contradiction handling 的真实策略
- forgetting 调度机制

但在架构边界、系统层次和接口能力上，本报告结论已经足够稳定，可作为 OpenCortex 的对标与吸收材料。
