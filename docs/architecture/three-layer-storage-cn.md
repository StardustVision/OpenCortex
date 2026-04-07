# 三层存储

## 为什么它存在

OpenCortex 既要在大多数查询中提供快速召回，也要在少数需要深度检查的场景里保持内容的完整保真。三层模型将这两类需求分离开来，让检索继续在 Qdrant 中以最小载荷运行，同时由文件系统保存规范内容和更丰富的层级结构。

## 核心模型

在常规（稳定态）模型中，每条记录在 CortexFS 中是一个目录，内含三个文件。例外是对话的即时层记录，这类数据直接写入 Qdrant，绕过 CortexFS，直到后续合并写入发生。

- `L0` 抽象：`.abstract.md`
- `L1` 概览：`.overview.md`
- `L2` 全文：`content.md`

Qdrant 存储向量，以及检索时需要用到的载荷（URI、abstract、overview、元数据、keywords 和相关字段）。`L0` 是默认用于向量化的文本，`L1` 是默认的检索细节层级，`L2` 仅在明确请求深度读取，或 planner/router 将细节层级提升到 `l2` 时才会读取。

## 写入路径

正常写入经过 `MemoryOrchestrator.add()`：

1. 通过 `IngestModeResolver` 判定写入模式：`memory`、`document`、`conversation`。
2. 如果有内容且记录是叶子节点，尝试用 `_derive_layers()` 从 `L2` 派生出 `L0`/`L1`/keywords；这是一个尽力而为的过程，在未配置 LLM 或派生失败时，会回退到调用方提供的字段（或保留空的 overview）。
3. 生成嵌入（默认文本为 `abstract`，可选组合 `abstract + keywords`）。
4. 向 Qdrant 执行带完整载荷和向量的 upsert 操作。
5. 以异步触发、不等待返回的方式调用 `CortexFS.write_context()`，将 `content.md`、`.abstract.md` 和 `.overview.md` 持久化。

Qdrant 是同步路径，CortexFS 故意异步写入，所以只要 `add` 成功，该记录就能被搜索到，即便文件写入稍微滞后或失败。

## 读取路径

检索由细节层级控制：

- `L0`：只取 abstract（默认来自 Qdrant 载荷，必要时再去 CortexFS 补 relation/parent 等上下文）。
- `L1`：abstract + overview（同样由 Qdrant 载荷提供，也可结合 CortexFS 中的关系信息补强上下文）。
- `L2`：在 Qdrant 拿到 abstract/overview，同时还从 CortexFS 拉 `content.md`。

默认检索细节层级为 `L1`，因此大多数请求都不会直接读取 `content.md`。只有在显式请求更高细节、planner/router 将细节层级提升到 `l2`，或者直接读取内容的端点需要加载 `content.md` 时，系统才会读取 `L2`。

## CortexFS 和 Qdrant 的关系

这是一个双写系统：

- **Qdrant**：快速检索层，保存向量和高频使用的载荷字段。
- **CortexFS**：规范内容层，保存全文、分层摘要和关系文件。

写入顺序是先 Qdrant、后 CortexFS。这样即便文件系统写入延迟或失败，检索仍然可靠；相应的代价是，`L2` 在 Qdrant 与 CortexFS 之间需要经过一段时间才能达到最终一致。

## 文档与对话的影响

文档模式会把输入解析成层级分块（`ParserRegistry`），每个分块都走同样的 `add()` 流程，因此每个分块会在 Qdrant 里有 `L0`/`L1`，在 CortexFS 有 `L2`。文档的元数据（源文档 id/标题、章节路径、分块角色）写入 Qdrant 的载荷，以便后续定位特定文档范围。

对话模式采用两层写入流程：

- **即时层**：逐条消息生成嵌入并直接写入 Qdrant（`meta.layer = "immediate"`），完全跳过 LLM 派生和 CortexFS，以实现快速、低延迟的召回。
- **合并层**：达到 token 阈值后，把缓冲的消息合并再通过 `add()` 写入（`meta.layer = "merged"`），恢复完整三层（`L0`/`L1`/`L2`）的写入流程。完成后会删除先前的即时层记录。

因此对话可以在 `L0` 级别立刻被检索，但只有在合并后 `L2` 内容才可用。

## 限制与权衡

- 双写方案增加了运维复杂度，需要接受 Qdrant 与 CortexFS 之间的最终一致性窗口。
- 默认返回 `L1` 避免了文件系统 I/O 和巨量 token 的载荷，但想获取全文需要显式请求深度读取或 planner/router 升级到 `l2`。
- 文档与对话的分块/合并流程保持了检索模型的一致性，但也让写路径更复杂、更依赖后台任务。

## 当前状态

三层存储是记忆、文档采集与对话采集共享的核心持久化模型。默认检索细节层级为 `L1`，`L2` 仅按需拉取。系统针对以 Qdrant 为先的快速召回做了优化，CortexFS 则作为规范内容的持久化承载，而不是一个必须同步完成的依赖。
