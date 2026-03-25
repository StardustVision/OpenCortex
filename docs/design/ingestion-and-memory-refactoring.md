# OpenCortex Ingestion & Memory Refactoring Design

## 1. 目标与背景

当前 OpenCortex 在处理复杂长文本（特别是多轮对话 Session 和长篇文档）时，暴露出两个核心问题：
1.  **QA 召回命中率低（0 命中多）**：对于 LoCoMo 等对话数据，系统倾向于将整个 Session 存为一条粗粒度的 memory。这导致向量密度极低，检索时由于缺乏细节特征，无法准确定位到对话中的具体事实和证据。
2.  **检索链路慢**：目前的性能瓶颈主要卡在 IntentRouter 阶段的 LLM 意图识别上（耗时 8s-22s），而底层的 Qdrant 向量检索通常只需几十毫秒。

**设计目标**：
在保持上层 API（如 `memory_store`, `batch_store`）和 MCP Tool“用户无感”的前提下，重构服务端的摄入（Ingestion）和存储逻辑。
核心思路是从“单调记录”向**“基于树状结构的细粒度情景归档”**与**“多维语义信息提取”**演进，彻底解决对话与长文档场景下的低命中率问题。

## 2. 总体方案概述

系统将引入一个新的内部组件 `IngestModeResolver`。当外部数据通过 `add` 或 `batch_add` 进入 Orchestrator 时，Resolver 将分析数据的元特征和内容，将其智能路由到三种不同的摄入模式（Mode）：

1.  **Memory 模式**：适用于偏好、实体、短事件等离散的、已高度凝练的事实。保持现有“一条输入存一条记录”的逻辑。
2.  **Document 模式**：适用于批量知识、长文档。触发按文档层级的自动化 Chunking，并利用 Hierarchical Retriever 的能力建立目录与段落的父子树。
3.  **Conversation / Session 模式（本次重构核心）**：适用于多轮对话和 Transcript。触发**“多轨并发处理工作流”**，一方面将对话切分为滑窗 Chunk 建立情景树，另一方面异步提取结构化的语义知识（如偏好、实体）。

## 3. 详细设计

### 3.1 路由层：IngestModeResolver

在 `MemoryOrchestrator.add` 内部增加一个轻量级的判断逻辑。

**判定规则顺序**：
1.  **显式信号优先**：如果 `meta` 中带有 `ingest_mode` 字段，则强制使用。如果来源是 `batch_store` 或包含 `source_path`/`scan_meta`，则默认走 **Document** 模式。如果存在 `session_id`，则默认走 **Conversation** 模式。
2.  **内容特征兜底**：通过简单正则或特征分析，发现内容包含明显的对话轮次结构（如 `User: ... \n Assistant: ...`），则走 **Conversation**；若具有明显章节特征的长文本，则走 **Document**；其他默认走 **Memory**。

### 3.2 存储侧深度优化：Session 数据的“多轨处理”

当数据被判定为 **Conversation** 或 **Document** 模式时，不再直接调用底层的单条存储，而是触发对应的流水线。

#### 3.2.1 第一轨：情景归档 (Episodic Track) —— 建立 Session 树

为了解决“细节找不到”和“宏观无总结”的矛盾，我们将单篇对话/文档物理拆分，但在逻辑上建立父子关系：

*   **滑窗切分 (Sliding Window Chunking)**：
    *   将对话按轮次（例如：窗口=4轮，步长=2轮）切分成多个局部 Chunk。
    *   滑窗保证了上下文的连续性，防止“指代消解”问题导致关键信息在切分边界断裂。
*   **局部提炼**：
    *   并行调用 LLM，为每个 Chunk 生成高密度的 `abstract`、`overview` 和 `keywords`。
*   **全局合并与建树 (Hierarchical Structure)**：
    1.  **生成父节点 (Session Level)**：将所有 Chunk 的核心内容汇总，生成一个全局的 Session 级摘要，作为 `is_leaf=False` 的目录节点存入。
    2.  **挂载子节点 (Chunk Level)**：将生成的各个 Chunk 作为 `is_leaf=True` 的叶子节点，并将其 `parent_uri` 指向上述父节点。

*优势：当用户提问宏观问题时，系统命中父节点并直接返回总结；当用户提问细节事实时，系统穿透到具体的 Chunk 召回精准证据。*

#### 3.2.2 第二轨：语义提取 (Semantic Track) —— “一鱼多吃”

一个 Session 不仅仅是一段录音，它包含了用户的特定偏好和系统状态。在第一轨建立情景树的同时（或异步），启动知识抽取流程：

1.  将 Session 内容通过特定的提取 Prompt (`Session-Extractor-Prompt`)，要求 LLM 输出 JSON 格式的结构化事实。
2.  **提取目标**：
    *   `preferences`：用户的偏好习惯。
    *   `entities`：项目名、IP地址、环境配置等静态状态。
    *   `patterns`：可复用的排错经验或操作流。
3.  **回写链接**：将这些提取出的“零散记忆”作为独立的 `memory` 模式数据存入，但在其 `meta` 中记录其来源的 `session_id` 和特定的 `chunk_uri`，实现从“语义记忆”到“情景记忆”的溯源。

### 3.3 检索侧优化

为了配合更丰富的底层数据结构，检索链路也需要同步微调：

1.  **向量化文本拓展**：
    当前 `Context.get_vectorization_text()` 仅使用 `abstract`。对于 Document 和 Conversation 生成的节点，应修改为返回 `abstract + overview + keywords`，大幅增加检索目标的面。
2.  **词法检索对齐**：
    当前 Qdrant 的词法后备 (`search_lexical`) 已经支持对 abstract/overview/keywords 的重叠度打分。将向量化文本拓展后，Dense Search 和 Sparse/Lexical Search 的数据基础达到了完美对齐。
3.  **Intent Router 性能优化（针对 8-22s 延迟问题）**：
    *   **强化 Zero-LLM 直通**：大幅扩充硬关键词 (`_detect_hard_keywords`) 和无召回场景 (`_NO_RECALL_PATTERNS`) 规则。对于简单的事实查找、路径检索，直接赋予默认参数跳过 LLM 分析。
    *   **Prompt 瘦身与级联架构 (Two-Stage Routing)**：
        *   当前 LLM 延迟高的主因是要求模型生成复杂的 `trigger_categories` 和多余的默认参数。需要将 Prompt 缩减为只输出最核心的 `should_recall`, `intent_type`, `time_scope`。
        *   引入**分层级联 (Cascade Filtering)**：先用极快的小模型（或正则）作为“守门员”仅判断 `should_recall`（True/False），一旦为 False 立即截断返回；只有为 True 时，才触发真正的意图和范围判断，避免盲目并发带来的 Token 浪费和网络长尾效应。
    *   **Intent 缓存**：对近期高频/类似的 Query 意图分类结果建立基于 TTL 的短时缓存。

## 4. 实施规划 (分三期落地)

### Phase 1: 核心路由与向量密度提升 (最小可行版本)
*   新增轻量级 `IngestModeResolver`。
*   修改 `Context.get_vectorization_text()`，支持组合字段 (`abstract + overview + keywords`) 向量化。
*   在 `MemoryOrchestrator` 中拦截，对于大块文本（特别是现有的批量测试集导入）临时加入简单的段落切分逻辑，验证 Chunking 对命中率的提升。

### Phase 2: 构建 Session 树与滑窗切分 (解决 0 命中)
*   实现正式的滑窗切分算法 (Sliding Window)。
*   实现 Session 数据的“父节点概括”与“子节点挂载”逻辑。
*   通过 `HierarchicalRetriever` 验证父子树结构的检索召回效果。

### Phase 3: 语义提取与性能优化
*   实现并测试 Session 的异步知识提取 Prompt (`Session-Extractor-Prompt`)。
*   完成从实体/偏好记忆到源 Session Chunk 的溯源链接（`related_uri` / `meta` 绑定）。
*   优化 Intent Router，加入更丰富的 Zero-LLM 直通规则，降低平均检索时延。

## 5. 预期收益
1.  **QA 场景 0 命中大幅下降**：通过滑窗 Chunking，局部事实的向量密度极度增加。
2.  **宏观与微观兼顾**：依赖于父子树结构，系统既能回答“上次聊了什么”（父节点概括），也能精准回答“上次说的 IP 是多少”（子节点穿透）。
3.  **认知能力升维**：通过独立提取偏好和实体，OpenCortex 从单一的“日志检索器”进化为具备“语义认知”与“情景溯源”能力的智能体记忆核心。