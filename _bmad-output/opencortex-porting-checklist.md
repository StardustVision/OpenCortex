# OpenCortex 移植清单 -- OpenViking 源码分析与移植指南

**版本：** v1.0
**日期：** 2026-02-26
**研究员：** Researcher Agent
**数据来源：** OpenViking 已安装包（pipx）运行时日志、本地数据目录、brainstorming 文档、collection schema、GitHub 仓库公开信息

---

## 1. OpenViking 目录结构总览 + 移植标记

以下结构基于运行时栈追踪（`ov_serve.log`）、本地数据目录分析、及 OpenViking 公开仓库结构推断。

```
openviking/
├── __init__.py
├── core/                          # 核心基础设施
│   ├── __init__.py
│   ├── client.py                  # OpenViking Client 入口        ✅ 需要移植
│   ├── engine.py                  # 记忆引擎，核心编排              ✅ 需要移植
│   ├── directories.py             # 目录结构定义与初始化            ✅ 需要移植
│   ├── config.py                  # 配置加载与管理                  ✅ 需要移植
│   └── filesystem.py              # VikingFS 路径解析/URI scheme    ✅ 需要移植
│
├── storage/                       # 存储后端层
│   ├── __init__.py
│   ├── viking_fs.py               # VikingFS 主实现（AGFS 集成）    ✅ 需要移植（核心）
│   ├── vectordb/                  # VectorDB 后端抽象 + 实现
│   │   ├── __init__.py
│   │   ├── base.py                # VectorDB 抽象接口              ✅ 需要移植（核心）
│   │   ├── local.py               # 本地 VectorDB 后端             ✅ 需要移植
│   │   ├── http.py                # HTTP 远程 VectorDB 后端        🔄 需要改造
│   │   ├── volcengine.py          # 火山引擎专有后端                ❌ 不需要
│   │   └── vikingdb.py            # VikingDB 专有后端              ❌ 不需要
│   ├── queuefs/                   # 队列文件系统
│   │   ├── __init__.py
│   │   ├── named_queue.py         # 命名队列（Embedding/Semantic）  ⚠️ 需要裁剪
│   │   └── queue_manager.py       # 队列管理器                     ⚠️ 需要裁剪
│   └── collection.py              # Collection schema 定义         ✅ 需要移植
│
├── retrieve/                      # 检索模块
│   ├── __init__.py
│   ├── retriever.py               # 检索主入口                     ✅ 需要移植（核心）
│   ├── strategies.py              # 检索策略实现                    ✅ 需要移植（核心）
│   ├── context_builder.py         # 上下文构建器                    ✅ 需要移植
│   └── recursive.py               # 目录递归检索                    ✅ 需要移植
│
├── session/                       # Session 管理
│   ├── __init__.py
│   ├── manager.py                 # Session 生命周期管理            ✅ 需要移植
│   ├── state.py                   # Session 状态追踪               ✅ 需要移植
│   └── memory_extract.py          # 记忆提取（从对话中提取记忆）     ✅ 需要移植（核心）
│
├── models/                        # Embedding 模型封装
│   ├── __init__.py
│   ├── embedding.py               # Embedding 模型接口             🔄 需要改造
│   ├── ark_embedding.py           # 火山方舟 Embedding             ❌ 不需要
│   └── openai_embedding.py        # OpenAI 兼容 Embedding          ✅ 需要移植
│
├── parse/                         # 文件解析
│   ├── __init__.py
│   ├── parser.py                  # 文件解析器接口                  ⚠️ 需要裁剪
│   └── markdown_parser.py         # Markdown 解析器                ✅ 需要移植
│
├── service/                       # 服务层
│   ├── __init__.py
│   ├── core.py                    # 核心服务（initialize 方法）     🔄 需要改造
│   └── memory_service.py          # 记忆服务                       ✅ 需要移植
│
├── server/                        # HTTP 服务器
│   ├── __init__.py
│   ├── app.py                     # FastAPI 应用（lifespan）        🔄 需要改造
│   └── routes/                    # API 路由
│       ├── memory.py              # 记忆 API                       🔄 需要改造
│       └── session.py             # Session API                    🔄 需要改造
│
├── mcp/                           # MCP 集成
│   ├── __init__.py
│   └── tools.py                   # MCP Tools 定义                 🔄 需要改造
│
└── utils/                         # 工具函数
    ├── __init__.py
    ├── logging.py                 # 日志工具                        ✅ 需要移植
    └── helpers.py                 # 通用辅助函数                    ✅ 需要移植

# 外部依赖包（非 openviking 仓库内，但紧密关联）
pyagfs/                            # AGFS 文件系统客户端
├── client.py                      # AGFS HTTP Client              🔄 需要改造（核心依赖）
├── exceptions.py                  # 异常定义                       ✅ 需要移植
└── ...

# C++ 扩展（src/ 目录）
src/
├── vectordb/                      # C++ 向量索引
│   ├── flat_index.cpp             # 平坦索引                       ❌ 不需要（pure Python 替代）
│   ├── hnsw_index.cpp             # HNSW 索引                     ❌ 不需要（pure Python 替代）
│   └── quant.cpp                  # 向量量化                       ❌ 不需要（pure Python 替代）
├── storage/                       # C++ 存储引擎
│   └── local_store.cpp            # 本地向量存储                   ❌ 不需要（pure Python 替代）
└── bindings/                      # Python 绑定
    └── pybind_module.cpp          # pybind11 绑定层               ❌ 不需要
```

### 移植标记汇总

| 标记 | 含义 | 模块数 |
|------|------|--------|
| ✅ 需要移植 | 核心功能，原样或微调移植 | ~22 |
| ⚠️ 需要裁剪 | 移植但去掉专有/非必要部分 | ~3 |
| ❌ 不需要 | volcengine 专有、C++ 扩展 | ~7 |
| 🔄 需要改造 | 接口保留但实现需调整 | ~8 |

---

## 2. VectorDB 后端抽象接口完整定义

### 2.1 Collection Schema（15 字段）

从 `/data/vectordb/context/collection_meta.json` 精确提取：

| # | FieldName | FieldType | 说明 | IsPrimaryKey |
|---|-----------|-----------|------|-------------|
| 0 | `id` | `string` | 唯一标识符 | **Yes** |
| 1 | `uri` | `path` | VikingFS URI（如 `viking://user/memories/entities/xxx`） | No |
| 2 | `type` | `string` | 节点类型（directory/file/memory 等） | No |
| 3 | `context_type` | `string` | 上下文类型（abstract/overview/content） | No |
| 4 | `vector` | `vector` (Dim=1024) | Dense 向量嵌入 | No |
| 5 | `sparse_vector` | `sparse_vector` | 稀疏向量（BM25/SPLADE 等） | No |
| 6 | `created_at` | `date_time` | 创建时间 | No |
| 7 | `updated_at` | `date_time` | 更新时间 | No |
| 8 | `active_count` | `int64` | 活跃计数（用于热度/衰减） | No |
| 9 | `parent_uri` | `path` | 父节点 URI（目录层级关系） | No |
| 10 | `is_leaf` | `bool` | 是否为叶子节点 | No |
| 11 | `name` | `string` | 节点名称 | No |
| 12 | `description` | `string` | 节点描述 | No |
| 13 | `tags` | `string` | 标签（逗号分隔或 JSON） | No |
| 14 | `abstract` | `string` | 摘要文本（L0 层内容） | No |

**索引配置：**
- **VectorIndex:** type=`flat`, dimension=`1024`, distance=`ip`(inner product), quant=`int8`, normalizeVector=`true`
- **ScalarIndex:** 对 `uri`, `type`, `context_type`, `created_at`, `updated_at`, `active_count`, `parent_uri`, `is_leaf`, `name`, `tags` 建标量索引

### 2.2 VectorDB 后端抽象接口（方法签名）

基于 brainstorming 文档确认和 OpenViking 公开 API 推断：

```python
class VectorDBBackend(ABC):
    """VectorDB 后端抽象接口 — OpenViking 定义的标准契约"""

    @abstractmethod
    def create_collection(
        self,
        collection_name: str,
        fields: list[FieldSchema],
        description: str = "",
        vector_index: VectorIndexConfig | None = None,
    ) -> None:
        """创建 Collection"""
        ...

    @abstractmethod
    def drop_collection(self, collection_name: str) -> None:
        """删除 Collection"""
        ...

    @abstractmethod
    def upsert(
        self,
        collection_name: str,
        data: list[dict[str, Any]],
    ) -> UpsertResult:
        """
        插入或更新向量记录。
        data 中每条记录包含 collection schema 中的字段。
        返回 UpsertResult(success_count, failed_ids)。
        """
        ...

    @abstractmethod
    def search(
        self,
        collection_name: str,
        vector: list[float],
        sparse_vector: dict[int, float] | None = None,
        filter: dict[str, Any] | None = None,
        top_k: int = 10,
        output_fields: list[str] | None = None,
    ) -> list[SearchResult]:
        """
        向量检索。
        支持 dense + sparse 混合检索。
        filter 支持标量过滤（uri, type, context_type, parent_uri, is_leaf, tags 等）。
        返回 SearchResult(id, score, fields) 列表。
        """
        ...

    @abstractmethod
    def delete(
        self,
        collection_name: str,
        ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> int:
        """
        删除向量记录。
        按 id 列表或 filter 条件删除。
        返回删除记录数。
        """
        ...

    @abstractmethod
    def update_uri(
        self,
        collection_name: str,
        old_uri: str,
        new_uri: str,
    ) -> int:
        """
        更新 URI（重命名/移动节点时使用）。
        批量更新所有匹配 old_uri 前缀的记录。
        返回更新记录数。
        """
        ...

    @abstractmethod
    def fetch(
        self,
        collection_name: str,
        ids: list[str],
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        按 ID 精确获取记录。
        """
        ...

    @abstractmethod
    def count(
        self,
        collection_name: str,
        filter: dict[str, Any] | None = None,
    ) -> int:
        """
        统计记录数。
        """
        ...
```

### 2.3 四种后端实现

| 后端 | 说明 | 移植决策 |
|------|------|---------|
| `local` | 本地文件系统存储，纯 Python + C++ 扩展索引 | ✅ 移植（替换 C++ 为 pure Python） |
| `http` | HTTP 远程后端（对接 AGFS server） | 🔄 改造（适配 OpenCortex 远程架构） |
| `volcengine` | 火山引擎 VikingDB 云服务 | ❌ 不需要 |
| `vikingdb` | VikingDB 专有客户端 | ❌ 不需要 |

---

## 3. L0/L1/L2 内容生成管道

### 3.1 三层内容模型

从本地数据目录（`data/viking/`）实际验证的三层结构：

| 层 | 文件名 | 大小 | 用途 | 示例 |
|----|--------|------|------|------|
| **L0 (abstract)** | `.abstract.md` | ~100 tokens | 一句话摘要，用于快速过滤 | "User scope. Stores user's long-term memory, persisted across sessions." |
| **L1 (overview)** | `.overview.md` | ~2K tokens | 结构化概述，用于理解上下文 | "Use this directory to access user's personalized memories. Contains three main categories: 1) preferences... 2) entities... 3) events..." |
| **L2 (content)** | 完整文件内容 | 不限 | 原文全文 | 完整的 Markdown 文档内容 |

### 3.2 内容生成管道逻辑

**写入流程（write_context）：**

```
输入: 原始内容(L2) + URI + metadata
    │
    ▼
1. AGFS mkdir(path)                      # 在文件系统中创建目录结构
    │                                     # （通过 pyagfs.client 调用 AGFS server）
    ▼
2. 解析原始内容                           # parse/ 模块
    │
    ▼
3. 生成 L1 (.overview.md)                # LLM 调用：压缩原文到 ~2K tokens
    │                                     # 保留结构化信息（分类、关系、关键词）
    ▼
4. 生成 L0 (.abstract.md)               # LLM 调用：进一步压缩到 ~100 tokens
    │                                     # 一句话总结核心语义
    ▼
5. 生成 Embedding 向量                   # models/ 模块
    │                                     # Dense: 1024 维（volcengine ark / openai 兼容）
    │                                     # Sparse: 可选（BM25/SPLADE）
    ▼
6. 写入 AGFS 文件系统                    # storage/viking_fs.py
    │  - 写 .abstract.md (L0)
    │  - 写 .overview.md (L1)
    │  - 写完整内容 (L2)
    ▼
7. Upsert VectorDB                       # storage/vectordb/
    │  - id, uri, type, context_type
    │  - vector (1024d), sparse_vector
    │  - created_at, updated_at, active_count
    │  - parent_uri, is_leaf, name, description, tags, abstract
    ▼
8. 入队 Named Queue                      # storage/queuefs/named_queue.py
    - Embedding queue（异步向量化）
    - Semantic queue（异步语义处理）
```

**关键实现细节：**

- `viking_fs.py` (line ~1140-1161 根据栈追踪)：`write_context()` 方法通过 `self.agfs.mkdir(path)` 创建目录，然后写入三层文件
- `directories.py` (line ~166-287)：`initialize_all()` 和 `_ensure_directory()` 负责初始目录结构创建，`_create_agfs_structure()` 写入初始 L0/L1 内容
- `service/core.py` (line ~218)：`initialize()` 调用 `directory_initializer.initialize_all()`
- `server/app.py` (line ~58)：FastAPI lifespan 中调用 `service.initialize()`

### 3.3 目录层级结构（已验证）

```
viking://
├── session/                    # Session 作用域（临时，可归档）
│   ├── .abstract.md           # L0: "Session scope..."
│   └── .overview.md           # L1: "Session-level temporary data..."
│
├── user/                       # User 作用域（持久化，跨 session）
│   ├── .abstract.md           # L0: "User scope..."
│   ├── .overview.md           # L1: "User-level persistent data..."
│   └── memories/
│       ├── .abstract.md       # "User's long-term memory storage..."
│       ├── .overview.md       # "Use this directory to access..."
│       ├── preferences/       # 用户偏好（按主题组织）
│       │   ├── .abstract.md   # L0
│       │   └── .overview.md   # L1
│       ├── entities/          # 实体记忆（项目、人、概念）
│       │   ├── .abstract.md   # L0
│       │   └── .overview.md   # L1
│       └── events/            # 事件记录（历史不可变）
│           ├── .abstract.md   # L0
│           └── .overview.md   # L1
│
├── agent/                      # Agent 作用域（全局学习）
│   ├── .abstract.md
│   ├── .overview.md
│   ├── memories/
│   │   ├── cases/             # 案例记录
│   │   └── patterns/          # 有效模式（跨交互提炼）
│   ├── instructions/          # 行为指令集
│   └── skills/                # 技能注册表（Claude Skills 协议）
│
├── resources/                  # 资源作用域（独立知识库）
│   ├── .abstract.md
│   └── .overview.md
│
└── transactions/               # 事务作用域
    ├── .abstract.md
    └── .overview.md
```

---

## 4. 检索模块实现细节

### 4.1 检索 5 步策略

基于 OpenViking 的检索管道，推断完整的 5 步策略：

```
Step 1: Query 分析与向量化
    │  - 对用户 query 生成 dense embedding (1024d)
    │  - 可选：生成 sparse vector（关键词级别）
    │  - 分析 query 意图，确定目标作用域（session/user/agent/resources）
    ▼
Step 2: L0 快速过滤（abstract 级别检索）
    │  - 在 VectorDB 中搜索，filter 条件：
    │    - context_type = "abstract"
    │    - parent_uri 限定作用域（如 /user/memories/）
    │    - is_leaf 根据需要过滤
    │  - 返回 Top-K 候选（K 通常 20~50）
    │  - 延迟目标: <50ms
    ▼
Step 3: L0 结果评分与排序
    │  - 对 L0 候选按 vector similarity 排序
    │  - 结合 active_count 热度加权
    │  - 过滤低分结果（similarity threshold）
    │  - 缩减到 Top-N（N 通常 5~10）
    ▼
Step 4: L1 展开（overview 级别）
    │  - 对 Top-N 结果加载 .overview.md 内容
    │  - 可选：对 L1 内容做二次相关性评估
    │  - 延迟目标: <200ms
    ▼
Step 5: L2 按需展开（完整内容）
    │  - 用户或系统请求时加载完整原文
    │  - 通过 AGFS 文件系统读取
    │  - 延迟目标: <100ms
    │  - 总端到端延迟目标: <500ms
    ▼
返回: RetrieveResult {
    nodes: list[NodeSummary],          # 排序后结果
    expandable_count: int,             # 可展开到 L2 的数量
    has_new_unverified: bool,          # exploration slot 标记
    recommended_depth: "L0"|"L1"|"L2"  # 推荐展开深度
}
```

### 4.2 目录递归检索

```python
# retrieve/recursive.py 核心逻辑推断

async def recursive_retrieve(
    uri: str,                          # 起始 URI（如 viking://user/memories/）
    query_vector: list[float],         # 查询向量
    depth: int = -1,                   # 递归深度（-1 = 无限）
    context_type: str = "abstract",    # 初始检索层级
    top_k: int = 10,                   # 每层返回数
    filter: dict | None = None,       # 额外过滤条件
) -> list[RetrieveResult]:
    """
    从指定 URI 开始递归检索：
    1. 在当前 URI 下搜索匹配的 abstract
    2. 对匹配的子目录递归深入
    3. 聚合并排序所有结果
    """
    ...
```

### 4.3 检索过滤条件

| 过滤维度 | 字段 | 示例 |
|---------|------|------|
| 作用域限定 | `parent_uri` | `/user/memories/entities/` |
| 内容层级 | `context_type` | `abstract` / `overview` / `content` |
| 节点类型 | `type` | `directory` / `file` / `memory` |
| 时间范围 | `created_at` / `updated_at` | 最近 7 天 |
| 热度阈值 | `active_count` | >= 5 |
| 标签过滤 | `tags` | 包含 `python` |
| 叶子节点 | `is_leaf` | true / false |

---

## 5. C++ 扩展功能评估与替代方案

### 5.1 C++ 扩展功能清单

| 功能 | 说明 | 性能要求 |
|------|------|---------|
| **Flat Index** | 暴力搜索向量索引 | 小数据集 (<10K) 足够 |
| **HNSW Index** | 近似最近邻搜索索引 | 大数据集 (>10K) 必需 |
| **Int8 Quant** | 向量量化（float32 -> int8） | 内存优化，精度损失可接受 |
| **Local Store** | 本地向量持久化存储 | 顺序读写 |

### 5.2 Pure Python 替代方案

| C++ 功能 | Python 替代 | 库 | 性能对比 | POC 可用性 |
|---------|------------|-----|---------|-----------|
| Flat Index | numpy 暴力搜索 | `numpy` | ~5x 慢（<10K 可接受） | 完全可用 |
| HNSW Index | hnswlib Python 绑定 | `hnswlib` | ~接近（预编译 wheel） | 完全可用 |
| HNSW Index | FAISS Python | `faiss-cpu` | ~接近 | 完全可用 |
| Int8 Quant | numpy 量化 | `numpy` | ~2x 慢 | 完全可用 |
| Local Store | LanceDB | `lancedb` | 更好（原生向量优化） | **推荐** |
| Local Store | ChromaDB | `chromadb` | 等效 | 备选 |

### 5.3 推荐替代策略

**POC 阶段（立即可用）：**
- 用 **LanceDB** 替代整个 C++ local store + 索引层
- LanceDB 内置 flat/IVF 索引、量化、持久化，零 C++ 编译
- 与 OpenCortex 现有代码中的 LanceDB 集成保持一致

**后续优化（按需）：**
- 若 LanceDB 性能不足，引入 `hnswlib` 或 `faiss-cpu` 做独立索引
- RuVector adapter 桥接后，向量存储和索引由 RuVector 接管

---

## 6. 关键模块详细分析

### 6.1 openviking/core/directories.py ✅ 需要移植

**功能：**
- 定义 OpenViking 的标准目录结构（session/user/agent/resources/transactions）
- `initialize_all()`: 启动时创建所有预定义目录
- `_ensure_directory()`: 确保单个目录存在
- `_create_agfs_structure()`: 为每个目录创建 L0(.abstract) + L1(.overview) 初始内容

**移植要点：**
- 目录定义（defn.abstract, defn.overview）原样保留
- AGFS 创建逻辑需适配（从 HTTP 远程改为本地文件系统直接操作）

### 6.2 openviking/storage/viking_fs.py ✅ 需要移植（核心）

**功能：**
- VikingFS 主实现，封装 AGFS 文件系统操作
- `write_context()`: 写入三层内容（L0/L1/L2）
- URI scheme 解析：`viking://` 前缀
- 依赖 `pyagfs.client.AGFSClient` 做文件 I/O
- 代码量大（至少 1161 行，根据栈追踪）

**移植要点：**
- 剥离 AGFS HTTP 依赖，改为本地文件系统直接操作
- 保留 URI scheme 和路径解析逻辑
- 保留三层内容生成管道

### 6.3 openviking/storage/queuefs/named_queue.py ⚠️ 需要裁剪

**功能：**
- 命名队列系统（Embedding queue, Semantic queue）
- 异步向量化和语义处理
- 依赖 AGFS server（localhost:1833）

**移植要点：**
- 队列概念保留但实现简化
- 用 Python asyncio 或 SQLite 队列替代 AGFS 队列
- 裁剪 AGFS 远程队列依赖

### 6.4 openviking/session/memory_extract.py ✅ 需要移植（核心）

**功能：**
- 从对话 turn 中提取记忆
- 记忆分类：preferences / entities / events
- LLM 调用提取结构化记忆

**移植要点：**
- 提取逻辑原样保留
- LLM 调用接口适配（保留 OpenAI 兼容接口）

### 6.5 openviking/service/core.py 🔄 需要改造

**功能：**
- 核心服务初始化（`initialize()` 方法）
- 协调 directories, viking_fs, vectordb, session 等模块
- 生命周期管理

**移植要点：**
- 保留初始化流程骨架
- 适配 OpenCortex 的配置系统
- 移除 volcengine 专有初始化

### 6.6 pyagfs (外部依赖) 🔄 需要改造

**功能：**
- AGFS 文件系统的 HTTP 客户端
- `mkdir()`, `read()`, `write()`, `delete()`, `list()` 等文件操作
- 连接 AGFS server（默认 localhost:1833）

**移植决策：**
- **不直接移植 pyagfs**
- 实现一个 `LocalAGFS` 适配层，提供相同接口但操作本地文件系统
- 后续可扩展为 HTTP 远程模式

---

## 7. 依赖关系分析

### 7.1 OpenViking 核心依赖

| 依赖 | 用途 | OpenCortex 是否需要 | 替代方案 |
|------|------|-------------------|---------|
| `pyagfs` | AGFS 文件系统客户端 | 🔄 需要改造 | 实现 `LocalAGFS`（本地文件操作） |
| `volcenginesdkarkruntime` | 火山方舟 SDK（Embedding/LLM） | ❌ 不需要 | `openai` SDK 兼容接口 |
| `fastapi` | HTTP 服务器框架 | ✅ 保留 | - |
| `uvicorn` | ASGI 服务器 | ✅ 保留 | - |
| `starlette` | ASGI 框架（FastAPI 依赖） | ✅ 自动引入 | - |
| `pydantic` | 数据验证与序列化 | ✅ 保留 | - |
| `requests` | HTTP 客户端 | ✅ 保留 | `httpx` (async 优先) |
| `urllib3` | HTTP 底层库 | ✅ 自动引入 | - |
| `numpy` | 向量运算 | ✅ 保留 | - |
| `lancedb` | 向量存储（OpenCortex 已选型） | ✅ 保留 | - |

### 7.2 OpenCortex 新增依赖

| 依赖 | 用途 | 必要性 |
|------|------|--------|
| `openai` | Embedding + LLM 调用 | 必须 |
| `tiktoken` | Token 计数 | 必须（Token Depth Controller） |
| `lancedb` | 向量存储 + 索引 | 必须 |
| `sqlite3` | 元数据、队列、状态机 | 内置 |
| `httpx` | 异步 HTTP 客户端 | 推荐（替代 requests） |
| `ruvector` | 强化学习排序 | 必须（adapter 桥接） |

### 7.3 模块间依赖图

```
server/app.py
    └── service/core.py
        ├── core/directories.py
        │   └── storage/viking_fs.py
        │       ├── pyagfs/client.py → 改为 LocalAGFS
        │       └── storage/queuefs/named_queue.py
        ├── storage/vectordb/base.py
        │   ├── storage/vectordb/local.py → 改为 LanceDB
        │   └── storage/collection.py
        ├── session/manager.py
        │   ├── session/state.py
        │   └── session/memory_extract.py
        │       └── models/embedding.py
        └── retrieve/retriever.py
            ├── retrieve/strategies.py
            ├── retrieve/recursive.py
            └── retrieve/context_builder.py
```

---

## 8. 移植优先级排序

### Phase 1.1 -- 地基层（第 1 天）

| 优先级 | 模块 | 任务 | 产出 |
|--------|------|------|------|
| P0 | `storage/vectordb/base.py` | 移植 VectorDB 抽象接口 | 接口定义 |
| P0 | `storage/collection.py` | 移植 Collection schema（15 字段） | Schema 定义 |
| P0 | `core/directories.py` | 移植目录结构定义 | 目录层级 |
| P0 | 新建 `LocalAGFS` | 实现本地文件系统操作（替代 pyagfs） | 文件系统层 |

### Phase 1.2 -- 内容存储层（第 2-3 天）

| 优先级 | 模块 | 任务 | 产出 |
|--------|------|------|------|
| P1 | `storage/viking_fs.py` | 移植 VikingFS（改用 LocalAGFS） | 三层内容管理 |
| P1 | `storage/vectordb/local.py` | 基于 LanceDB 实现 local 后端 | 向量存储 |
| P1 | `models/embedding.py` | 移植 Embedding 接口（OpenAI 兼容） | 向量化能力 |

### Phase 1.3 -- 检索层（第 4-5 天）

| 优先级 | 模块 | 任务 | 产出 |
|--------|------|------|------|
| P2 | `retrieve/retriever.py` | 移植检索主入口 | 检索能力 |
| P2 | `retrieve/strategies.py` | 移植 5 步检索策略 | 分层检索 |
| P2 | `retrieve/recursive.py` | 移植目录递归检索 | 递归检索 |
| P2 | `retrieve/context_builder.py` | 移植上下文构建 | 上下文组装 |

### Phase 1.4 -- Session 与记忆提取（第 6-7 天）

| 优先级 | 模块 | 任务 | 产出 |
|--------|------|------|------|
| P3 | `session/manager.py` | 移植 Session 管理 | Session 生命周期 |
| P3 | `session/state.py` | 移植 Session 状态 | 状态追踪 |
| P3 | `session/memory_extract.py` | 移植记忆提取 | 自动记忆抽取 |

### Phase 1.5 -- RuVector 桥接（第 8-10 天）

| 优先级 | 模块 | 任务 | 产出 |
|--------|------|------|------|
| P4 | 新建 `RuVectorAdapter` | 实现 VectorDB 接口 + 强化能力 | 桥接层 |
| P4 | 新建 `Memory Orchestrator` | 编排检索/写入/反馈/衰减 | 核心编排 |
| P4 | 端到端验证 | write -> L0/L1/L2 -> retrieve -> 排序 | 链路验证 |

### Phase 1.6 -- 服务层改造（第 11-14 天）

| 优先级 | 模块 | 任务 | 产出 |
|--------|------|------|------|
| P5 | `service/core.py` | 改造核心服务初始化 | 服务骨架 |
| P5 | `server/app.py` | 改造 FastAPI 应用 | HTTP API |
| P5 | `mcp/tools.py` | 改造 MCP Tools | MCP 集成 |
| P5 | `parse/markdown_parser.py` | 移植 Markdown 解析 | 文件解析 |

---

## 9. 关键风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| VikingFS 代码量大（>1000 行），移植工作量超预期 | 高 | 中 | 分层移植，先移植核心 write_context/read_context，其他按需 |
| pyagfs 替换为 LocalAGFS 时接口不兼容 | 中 | 低 | 先写接口适配层，运行时可切换 local/http 模式 |
| Collection schema 15 字段 + 索引配置移植遗漏 | 高 | 低 | 已从 collection_meta.json 精确提取，按此文档对照 |
| C++ 扩展裁剪后性能不足 | 中 | 低 | LanceDB 性能对 POC 规模（<100K 记录）绰绰有余 |
| L0/L1 生成依赖 LLM 调用，增加延迟和成本 | 中 | 中 | 写入时异步生成，不阻塞主流程；可配置是否启用 L1 |
| OpenViking 版本更新导致接口变化 | 低 | 中 | fork 后独立演进，定期对比上游变更 |

---

## 10. 移植后的 OpenCortex 目标目录结构

```
src/opencortex/
├── __init__.py
├── core/
│   ├── client.py                  # from openviking/core/client.py
│   ├── engine.py                  # from openviking/core/engine.py
│   ├── directories.py             # from openviking/core/directories.py
│   ├── config.py                  # 改造：OpenCortex 配置体系
│   └── filesystem.py              # from openviking/core/filesystem.py
├── storage/
│   ├── viking_fs.py               # from openviking/storage/viking_fs.py（改名考虑）
│   ├── local_agfs.py              # 新建：替代 pyagfs
│   ├── vectordb/
│   │   ├── base.py                # from openviking/storage/vectordb/base.py
│   │   ├── lancedb_backend.py     # 新建：LanceDB 实现
│   │   └── ruvector_adapter.py    # 新建：RuVector 桥接
│   ├── collection.py              # from openviking/storage/collection.py
│   └── queue.py                   # 简化：替代 queuefs
├── retrieve/
│   ├── retriever.py               # from openviking/retrieve/retriever.py
│   ├── strategies.py              # from openviking/retrieve/strategies.py
│   ├── recursive.py               # from openviking/retrieve/recursive.py
│   └── context_builder.py         # from openviking/retrieve/context_builder.py
├── session/
│   ├── manager.py                 # from openviking/session/manager.py
│   ├── state.py                   # from openviking/session/state.py
│   └── memory_extract.py          # from openviking/session/memory_extract.py
├── models/
│   ├── embedding.py               # 改造：OpenAI 兼容 Embedding
│   └── providers/                 # 多 provider 支持
├── orchestrator/
│   ├── memory_orchestrator.py     # 新建：核心编排（8 职责）
│   ├── feedback_analyzer.py       # 新建：反馈分析
│   └── token_depth.py             # 新建：Token 深度控制
├── parse/
│   └── markdown_parser.py         # from openviking/parse/markdown_parser.py
├── service/
│   └── core.py                    # 改造：OpenCortex 服务初始化
├── server/
│   ├── app.py                     # 改造：FastAPI 应用
│   └── routes/
├── mcp/
│   └── tools.py                   # 改造：MCP Tools
└── utils/
    ├── logging.py
    └── helpers.py
```

---

## 11. 废弃模块清单（Phase 1 旧代码）

根据 brainstorming 文档确认，以下 MemCortex Phase 1 模块将被 HRCM 架构替换：

| 废弃模块 | 原文件 | 替代方案 |
|---------|--------|---------|
| spool | `src/memcortex/spool.py` | OpenViking session + queue |
| engine | `src/memcortex/engine.py` | Memory Orchestrator |
| sync_client | `src/memcortex/sync_client.py` | MCP Server 直接集成 |
| vector_store | `src/memcortex/vector_store.py` | VectorDB backend (LanceDB + RuVector) |
| models | `src/memcortex/models.py` | OpenViking Session + MemoryEvent |
| embeddings | `src/memcortex/embeddings.py` | OpenViking models/embedding.py |

---

## 12. 下一步行动

1. **获取 OpenViking 完整源码** -- `gh repo clone volcengine/OpenViking` 到本地，逐文件阅读
2. **精确确认方法签名** -- 阅读 `storage/vectordb/base.py`，确认本文档中推断的接口是否完全准确
3. **评估 VikingFS 代码量** -- 确认 `viking_fs.py` 实际行数和复杂度
4. **验证 L0/L1 生成 prompt** -- 找到 LLM 调用的 prompt 模板
5. **确认检索 5 步策略** -- 阅读 `retrieve/strategies.py` 实际实现
6. **开始 Phase 1.1 移植** -- 从 VectorDB 接口和 Collection schema 开始

---

*注：本文档基于 OpenViking 运行时日志栈追踪、本地数据目录分析、brainstorming 设计文档、及项目公开信息推断。部分模块的内部实现细节（如精确方法签名、检索策略参数）需要在获取完整源码后进一步验证和补充。标注"推断"的部分应在源码阅读后更新。*
