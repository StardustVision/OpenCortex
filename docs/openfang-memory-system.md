# OpenFang 记忆系统实现文档 (Memory System)

## 1. 架构总览 (Architecture Overview)

OpenFang 的记忆系统旨在为 AI Agent 提供多模态、持久化且具备遗忘机制的上下文存储。该系统由 `openfang-memory` Crate 实现，通过一个统一的入口（Facade）—— `MemorySubstrate`，协调底层的四个专用子存储引擎（Stores）。

整个系统的持久化层建立在 **SQLite** 之上，为了保障高并发读写性能，默认开启了 `WAL` (Write-Ahead Logging) 模式和 `5000ms` 的忙等待超时。

系统实现了定义在 `openfang-types/src/memory.rs` 中的异步 `Memory` Trait。

## 2. 核心组件：统一基质 (MemorySubstrate)

`MemorySubstrate` 是整个记忆系统的“大脑”，它通过共享一个被 `Arc<Mutex<rusqlite::Connection>>` 包裹的 SQLite 连接，将不同维度的记忆能力聚合在一个统一的 API 下。

其内部主要协调以下引擎：
*   `StructuredStore`: 结构化键值存储
*   `SemanticStore`: 语义与向量存储
*   `KnowledgeStore`: 知识图谱存储
*   `SessionStore`: 会话与历史记录存储
*   `ConsolidationEngine`: 记忆巩固与遗忘引擎

`MemorySubstrate` 利用 `tokio::task::spawn_blocking` 将底层的同步 SQLite 操作包装为异步（Async）接口，从而与基于 Tokio 的异步运行时完美融合。

## 3. 四大子存储引擎 (Storage Engines)

### 3.1 结构化存储 (Structured Store)
*   **文件路径**: `structured.rs`
*   **作用**: 提供灵活的键值对（Key-Value）存取能力。
*   **特性**: 专门用于存储 Agent 的状态、配置和固定参数（以 `serde_json::Value` 格式存储）。它确保 Agent 重启后依然能恢复明确的工程状态。

### 3.2 语义存储 (Semantic Store)
*   **文件路径**: `semantic.rs`
*   **作用**: 处理非结构化的自然语言记忆片段 (`MemoryFragment`)。
*   **检索机制**:
    *   **Phase 1 (后备方案)**: 当没有提供向量时，使用 SQLite 原生的 `LIKE` 语句进行模糊文本匹配。
    *   **Phase 2 (核心机制)**: 支持将嵌入向量（Embeddings）序列化为字节流（BLOB）存入 SQLite。当查询带有 Query Embedding 时，系统会先放宽查询限制获取候选集（`fetch_limit = limit * 10`），然后通过**余弦相似度 (Cosine Similarity)** 在内存中进行精确重排（Re-ranking）。
*   **访问追踪**: 每次记忆被召回，都会自动执行 `UPDATE` 更新 `access_count` (访问次数) 和 `accessed_at` (最后访问时间)，这是后续遗忘机制的数据基础。

### 3.3 知识存储 (Knowledge Store)
*   **文件路径**: `knowledge.rs`
*   **作用**: 构建结构化的知识图谱，管理实体（`Entity`）与关系（`Relation`）。
*   **实现细节**: 
    *   底层使用 `entities` 和 `relations` 两张表模拟图数据库。
    *   支持图模式匹配查询 (`GraphPattern`)。通过 `JOIN` 语句（`relations JOIN entities` 两次分别映射 source 和 target），可以高效找出与特定实体或特定关系相连的节点。

### 3.4 会话存储 (Session Store)
*   **文件路径**: `session.rs`
*   **作用**: 管理对话历史。
*   **核心特性：Canonical Session (规范化会话)**：
    *   针对多渠道（如 Telegram, Discord 甚至 CLI）设计的跨平台持久化上下文。用户在任意平台的对话都会汇入唯一的 Canonical Session 中。
*   **核心特性：上下文压缩 (Compaction)**：
    *   为防止 Token 爆炸，系统设定了阈值（如默认 100 条）。
    *   当消息超过阈值时，触发 Compaction 机制：保留最新的 50 条消息（`DEFAULT_CANONICAL_WINDOW`），将其余的历史消息浓缩为一个文本摘要（`compacted_summary`）。
    *   **LLM 摘要增强**: 预留了 `store_llm_summary` 接口，允许外层调用大模型生成高质量的上下文总结，替代简单的截断式压缩。
*   **JSONL 镜像**: 提供了一个 `write_jsonl_mirror` 方法，将 SQLite 中的二进制对话记录，以人类可读的 `JSONL` 格式异步备份到磁盘，便于开发者调试和分析。

## 4. 记忆生命周期管理 (Consolidation & Decay)

为了防止记忆无限膨胀导致性能下降和无关信息干扰，系统引入了受“艾宾浩斯遗忘曲线”启发的管理机制。

*   **执行引擎**: `ConsolidationEngine` (`consolidation.rs`)
*   **遗忘机制 (Decay)**:
    *   扫描超过 7 天未被访问的记忆 (`accessed_at < 7 days ago`)。
    *   将这些记忆的置信度 (`confidence`) 按照 `decay_rate` (衰减率) 进行打折，最低降至 0.1。
    *   这一机制确保高频调用的核心记忆始终保持高权重，而陈旧信息自然沉底。
*   **拓展性**: 引擎内部结构已为下一阶段的“相似记忆融合（Merging）”做好了准备。

## 5. 任务队列 (Task Queue)

除了纯粹的“记忆”，`MemorySubstrate` 还客串了轻量级任务队列的角色：
*   支持 `task_post`, `task_claim`, `task_complete`, 和 `task_list`。
*   使不同的 Agent 或系统组件可以利用统一的底层存储进行异步的任务分发与状态同步。
