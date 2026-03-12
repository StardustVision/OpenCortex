# OpenCortex 摄入与检索管线优化 — 设计规格

## 1. 问题陈述

OpenCortex 在处理复杂长文本时存在三个核心问题：

1. **QA 基准测试零命中**：对话和文档以粗粒度单条记录存储，向量密度过低，无法匹配细粒度事实查询。LoCoMo 基准测试中全部 8 组完成的对话检索结果为 0。
2. **检索管线慢（8-22秒）**：IntentRouter 对每个查询都调用 LLM，即使是简单查询。底层 Qdrant 向量检索仅需几十毫秒。
3. **扁平文档存储**：`batch_store` / `oc-scan` 将每个文件存为独立记录，无目录层级，无法进行结构化导航。

### 已定位的根因

- **HierarchicalRetriever 目录过滤 Bug**：目录节点 `category=""`，但 wave search 应用完整的 `metadata_filter`（包含 `category=X`），过滤掉了树遍历所需的所有 `is_leaf=False` 节点。
- **单条记录摄入**：对话和文档无分块处理——无论长度如何，一条输入只存一条记录。
- **向量化文本过窄**：`Context.get_vectorization_text()` 仅返回 `abstract`，缺少 keywords 信号。
- **IntentRouter 无 session 感知**：每次查询都触发 LLM 分析，无快速路径。

## 2. 设计目标

1. **三模式智能摄入**：Memory（短事实）、Document（长文档+代码）、Conversation（多轮对话）——自动路由。
2. **对话模式两层增量分块**：每条消息立即可检索；达到 token 阈值后合并为高质量 chunk。
3. **层级化文档解析**：移植 OpenViking 多格式解析器套件，在 Qdrant 中构建父子树。
4. **代码库支持**：`oc-scan` + `batch_add` 从文件路径生成层级树。
5. **检索性能**：修复目录过滤 Bug，扩展向量化文本，通过多查询并发优化 IntentRouter。
6. **API 兼容**：现有 `store`、`batch_store`、`search`、`recall` API 不变。

## 3. 架构概览

```
                        ┌─────────────────────┐
                        │   store / batch_store │
                        │   add_message         │
                        └──────────┬────────────┘
                                   │
                        ┌──────────▼────────────┐
                        │   IngestModeResolver   │
                        │    （路由到三种模式）     │
                        └──┬───────┬─────────┬───┘
                           │       │         │
                    ┌──────▼──┐ ┌──▼──────┐ ┌▼──────────┐
                    │ Memory  │ │Document │ │Conversation│
                    │  模式   │ │  模式    │ │   模式     │
                    │（直通） │ │（解析    │ │（两层      │
                    │         │ │+分块）  │ │ 增量分块） │
                    └────┬────┘ └────┬────┘ └─────┬──────┘
                         │          │             │
                    ┌────▼──────────▼─────────────▼──────┐
                    │          Qdrant 存储                │
                    │     （父子树结构）                    │
                    └────────────────┬────────────────────┘
                                    │
                    ┌───────────────▼────────────────────┐
                    │      HierarchicalRetriever          │
                    │  （wave search + 目录过滤修复）      │
                    └───────────────┬────────────────────┘
                                    │
                    ┌───────────────▼────────────────────┐
                    │      IntentRouter（优化后）          │
                    │ （session 感知 → 多查询 → 缓存）    │
                    └────────────────────────────────────┘
```

## 4. 详细设计

### 4.1 IngestModeResolver

在 `MemoryOrchestrator.add()` 和 `batch_add()` 内部添加轻量级路由逻辑。

**判定优先级（显式信号优先）**：

| 优先级 | 信号 | 模式 |
|--------|------|------|
| 1 | `meta.ingest_mode = "memory"/"document"/"conversation"` | 强制指定 |
| 2 | `batch_store` 调用 或存在 `source_path`/`scan_meta` | Document |
| 3 | 存在 `session_id`（通过 `add_message`） | Conversation |
| 4 | 内容包含对话模式（`User:...`、`Assistant:...`） | Conversation |
| 5 | 内容有标题结构 + 长度 > 4000 tokens | Document |
| 6 | 默认 | Memory |

**去重策略**：全部三种模式统一关闭去重。`add()` 方法的 `dedup` 参数默认值从 `True` 改为 `False`。这是一个**行为变更**——原来依赖自动去重的调用方需显式传入 `dedup=True`。

### 4.2 Conversation 模式 — 两层增量分块

#### 4.2.1 即时层（Immediate Layer）

每次 `add_message` 调用时，`ContextManager._commit()` 调用一个新的轻量级 orchestrator 方法：

```python
# MemoryOrchestrator 上的新方法（绕过完整的 add() 流水线）
async def _write_immediate(self, session_id: str, msg_index: int, text: str) -> str:
    """写入单条消息以实现立即可检索。无 LLM，无 CortexFS。"""
    uri = self._auto_uri(category="events", context_type="memory")
    vector = await self._embed(text)
    record = {
        "id": uri_to_point_id(uri),
        "uri": uri,
        "vector": vector,
        "abstract": text[:500],
        "content": text,
        "parent_uri": f"opencortex://.../{session_id}",
        "is_leaf": True,
        "category": "events",
        "meta": {"layer": "immediate", "msg_index": msg_index, "session_id": session_id},
    }
    await self._storage.upsert("context", [record])
    return uri
```

关键决策：
- **绕过 `add()`**：无 LLM 推导、无 CortexFS 双写、无去重检查。
- **直接 `_storage.upsert`**：仅写入 Qdrant（不生成 `.abstract.md` / `.overview.md` 文件）。
- **URI 生成**：复用 `_auto_uri`，`category="events"`，保持 URI 格式一致。
- **跳过 CortexFS**：即时记录是临时的——会被合并块替换，无需文件系统持久化。

**累积缓冲区**：`ContextManager` 为每个 session 维护一个 `ConversationBuffer`：

```python
@dataclass
class ConversationBuffer:
    messages: List[str]          # 原始消息文本
    token_count: int = 0         # 累积 token 估算
    start_msg_index: int = 0     # 缓冲区中第一条消息的序号
    immediate_uris: List[str] = field(default_factory=list)  # 合并时用于删除的 URI 列表
```

Token 估算复用 OpenViking 方法：CJK 字符 × 0.7 + 其他字符 × 0.3。

**效果**：每条消息立即可通过向量检索命中。

#### 4.2.2 合并层（Merge Layer）

当缓冲区累积达到 ~1000 tokens 时（在每次 `_commit()` 结束时检查）触发：

1. **收集缓冲区消息**：自上次合并以来的所有消息。
2. **LLM 推导**：并行生成 `abstract` + `overview` + `keywords`（复用现有 `_derive_structured` prompt）。
3. **写入合并块**：通过完整 `orchestrator.add()` 流水线——新的 `is_leaf=True` 记录：
   - `abstract` / `overview` / `keywords` = LLM 生成
   - `content` = 拼接的原始对话文本
   - `parent_uri` = session 根节点
   - `meta.layer` = `"merged"`
   - `meta.msg_range` = `[start_idx, end_idx]`
4. **替换即时记录**：通过 URI 列表删除（`ConversationBuffer.immediate_uris`）——无需 schema 查询，内存中跟踪即可。
5. **重置缓冲区**。

合并以 fire-and-forget `asyncio.create_task` 方式运行，不阻塞 `_commit()` 响应。即时记录在合并完成并删除前保持可检索。

#### 4.2.3 Session 结束

`session_end` 时：

1. **刷新剩余缓冲区**：即使不到 1000 tokens 也强制合并。
2. **创建 Session 父节点**：汇总所有合并块摘要，生成一条 `is_leaf=False` 目录记录。
3. **触发 Alpha Pipeline**：Observer.flush → TraceSplitter → TraceStore → Archivist（现有流程）。

#### 4.2.4 数据流

```
add_message(msg_1)
  → embed(msg_1) → 写入即时记录 #1        [可检索]
  → 缓冲区: [msg_1], tokens: 200

add_message(msg_2)
  → embed(msg_2) → 写入即时记录 #2        [可检索]
  → 缓冲区: [msg_1, msg_2], tokens: 450

add_message(msg_3)
  → embed(msg_3) → 写入即时记录 #3        [可检索]
  → 缓冲区: [msg_1..3], tokens: 1050     [超过阈值]
  → 合并:
      LLM derive(msg_1+msg_2+msg_3) → abstract/overview/keywords
      写入合并块 (msg_range: [1,3])
      删除即时记录 #1, #2, #3
  → 缓冲区重置

add_message(msg_4)
  → embed(msg_4) → 写入即时记录 #4        [可检索]
  → 缓冲区: [msg_4], tokens: 180

session_end()
  → 刷新: 合并 [msg_4] → 合并块
  → 创建 session 父节点（汇总所有合并块）
  → Alpha pipeline
```

### 4.3 Document 模式 — 多格式层级解析

#### 4.3.1 解析器架构（移植自 OpenViking）

所有解析器遵循统一模式：转换为 Markdown → 委托 MarkdownParser 进行结构化切分。

**移植的解析器（Phase 1 + Code）**：

| 解析器 | 支持格式 | 依赖 | 说明 |
|--------|---------|------|------|
| MarkdownParser | .md | 无 | 核心：按标题层级结构化切分 |
| TextParser | .txt | 无 | 委托 MarkdownParser |
| WordParser | .docx | `python-docx` | 转 markdown → 委托 |
| ExcelParser | .xlsx/.xls/.xlsm | `openpyxl` | Sheet → markdown 表格 → 委托 |
| PowerPointParser | .pptx | `python-pptx` | Slide → markdown → 委托 |
| PDFParser | .pdf | `pdfplumber` | 书签/字体标题检测 → markdown → 委托 |
| EPubParser | .epub | `ebooklib`（可选） | HTML 提取 → markdown → 委托 |
| CodeRepositoryParser | git/zip/本地目录 | git CLI | 目录遍历，按文件出块 |

**延后（Phase 2+）**：HTMLParser（URL 抓取，readabilipy/markdownify/bs4）、Media 解析器。

#### 4.3.2 输出格式

解析器写入 CortexFS（替代 OpenViking 的 VikingFS）。输出格式为扁平 chunk 列表，由编排器写入 CortexFS（L2 内容）+ Qdrant：

```python
@dataclass
class ParsedChunk:
    content: str           # 原始文本内容
    title: str             # section 标题（无则为空字符串）
    level: int             # 层级深度（0=根）
    parent_index: int      # 父 chunk 在列表中的索引（-1=无）
    source_format: str     # 原始格式（markdown/docx/pdf/code...）
    meta: dict             # 附加元数据（file_path、file_type 等）
```

#### 4.3.3 分块参数（来自 OpenViking）

| 参数 | 值 | 说明 |
|------|-----|------|
| MAX_SECTION_SIZE | 1024 tokens | 单个 chunk 上限 |
| MIN_SECTION_TOKENS | 512 tokens | 低于此值则与相邻 section 合并 |
| 小文档阈值 | 4000 tokens | 低于此值不切分（走 Memory 模式） |
| Token 估算 | CJK: 0.7 token/字符, 其他: 0.3 token/字符 | 多语言感知 |

#### 4.3.4 切分策略（按优先级）

1. **小文档**（< 4000 tokens）：不切分，存为单条 Memory 记录。
2. **标题切分优先**：检测 markdown 标题（`#` ~ `######`），按标题层级递归切分。
3. **小 section 合并**：相邻 section < 512 tokens → 合并为一个 chunk（保持 ≤ 1024 tokens）。
4. **大 section 无子标题**：> 1024 tokens 且无子标题 → 按段落（`\n\n`）智能拆分。
5. **无标题长文本**：整体按段落切分，每段 ≤ 1024 tokens。

#### 4.3.5 代码库处理

代码库不走 MarkdownParser 结构化切分。改为：

```
oc-scan（客户端）
  → 扫描本地目录，读取文件
  → 发送 {items: [{content, meta.file_path}], source_path, scan_meta}
  → batch_store API

batch_add（服务端，增强后）
  → 检测到 scan_meta → Document 模式
  → 从 meta.file_path 相对路径构建目录树：
      src/                          → is_leaf=False 目录节点
      src/opencortex/               → is_leaf=False 目录节点
      src/opencortex/orchestrator.py → is_leaf=True 文件节点
  → 大文件（> MAX_SECTION_SIZE）→ 按函数/类边界切分（如有 AST）或按行切分
  → Phase 1: 并发处理所有叶子节点（asyncio.Semaphore(5) 限制 LLM 并发）
      → 文件节点：LLM 推导 abstract/overview/keywords
  → Phase 2: 自底向上处理目录节点（子节点必须先完成）
      → 目录节点：LLM 汇总子文件摘要
```

**并发模型**：叶子节点无依赖，可通过 Semaphore（默认 5 个并发 LLM 调用）并行处理。目录节点依赖子节点摘要，所以在所有子节点完成后自底向上处理。错误按条目收集并在响应中返回（与当前 `batch_add` 行为一致）。

**目录树示例**：

```
project-root (is_leaf=False, 整个项目摘要)
├── src/ (is_leaf=False, src 目录内容摘要)
│   ├── opencortex/ (is_leaf=False, 摘要)
│   │   ├── orchestrator.py (is_leaf=True, abstract + content)
│   │   ├── config.py (is_leaf=True)
│   │   └── http/ (is_leaf=False)
│   │       ├── server.py (is_leaf=True)
│   │       └── client.py (is_leaf=True)
├── tests/ (is_leaf=False)
│   └── test_e2e.py (is_leaf=True)
└── README.md (is_leaf=True)
```

#### 4.3.6 OpenViking 依赖 — 解耦方案

| OpenViking 依赖 | OpenCortex 替代方案 |
|-----------------|-------------------|
| `openviking.parse.base`（ParseResult, ResourceNode, NodeType） | 新建 `opencortex.parse.base`，包含 `ParsedChunk` + `format_table_to_markdown` + `lazy_import` |
| `openviking.parse.parsers.base_parser.BaseParser` | 新 `BaseParser`，返回 `List[ParsedChunk]` |
| `openviking_cli.utils.config.parser_config.ParserConfig` | 简化为 `@dataclass ParserConfig(max_section_size, min_section_tokens)` |
| `openviking_cli.utils.logger` | 标准 `logging.getLogger(__name__)` |
| `openviking.storage.viking_fs` | 替换为 `opencortex.storage.cortex_fs`（CortexFS） |
| `openviking.parse.parsers.upload_utils` | 保留 `should_skip_file`、`should_skip_directory`、`detect_and_convert_encoding` 工具函数 |
| `openviking.parse.parsers.constants` | 复制 `IGNORE_DIRS`、`IGNORE_EXTENSIONS`、`CODE_EXTENSIONS` 等 |
| `openviking_cli.utils.config.get_openviking_config` | 移除——`github_domains` 硬编码默认值 |
| `openviking.utils.is_github_url` / `parse_code_hosting_url` | 内联简化版 |

#### 4.3.7 处理流程（完整）

```
store(content, ingest_mode="document", meta={source_path: "report.pdf"})
  → IngestModeResolver → "document"
  → ParserRegistry.get_parser_for_file(".pdf") → PDFParser
  → PDFParser.parse("report.pdf")
    → pdfplumber → markdown 字符串
    → MarkdownParser.split(markdown) → List[ParsedChunk]
  → for each chunk（并行）:
      LLM derive → abstract / overview / keywords
  → 写入父节点 (is_leaf=False, 所有 chunk 的摘要汇总)
  → 写入 chunk_1..N (is_leaf=True, parent_uri=parent)
```

### 4.4 Memory 模式 — 直通

与当前行为不变：

- 单条输入 → 单条记录。
- 通过 `_derive_structured` 进行 LLM 推导（abstract + overview + keywords）。
- 去重：关闭（默认值从 `True` 改为 `False`）。
- 不分块、不建树。

### 4.5 检索侧优化

#### 4.5.1 向量化文本扩展

`Context` 类没有 `keywords` 属性。Keywords 由 orchestrator 中的 `_derive_layers()` 推导并作为 Qdrant payload 字段存储，但从未设置到 `Context` 对象上。`Vectorize` 对象在构造时仅用 `abstract` 初始化。

**实现方式**：修改 orchestrator 的 `add()` 流程，在 LLM 推导完成后、embedding 之前更新向量化文本：

```python
# 在 orchestrator.add() 中，_derive_layers() 返回之后（约第 739 行）：
abstract, overview, keywords = await self._derive_layers(content, ...)

# 在 embed() 调用之前（约第 766 行），更新向量化文本：
vectorize_text = f"{abstract} {keywords}" if keywords else abstract
ctx.vectorize = Vectorize(vectorize_text)

# 然后 embed() 使用扩展后的文本：
vector = await self._embed(ctx.get_vectorization_text())
```

这样 `Context` 类保持不变——修改完全在 orchestrator 的 `add()` 方法中。无需在 `Context` 上添加新属性。

Keywords 提供高密度检索信号。不加 overview 以避免稀释向量语义。

对所有模式（Memory、Document、Conversation）统一生效。

#### 4.5.2 HierarchicalRetriever 目录过滤修复

**Bug**：目录节点（`is_leaf=False`）的 `category=""`。当 `metadata_filter` 包含 `category=X` 时，目录节点被 wave search 排除，导致树遍历完全失败。

**现状**：`dir_friendly` OR 过滤包装模式已在 `hierarchical_retriever.py` 的 4 个位置应用（frontier batch、compensation query、tiny queries、`_recursive_search`）。这些修复需要通过 LoCoMo benchmark 重跑来**验证**。

**剩余缺口**：`_global_vector_search`（约第 294 行）是一个仅搜索目录的方法（已强制 `is_leaf=False`）。其他 4 个位置的 dir-friendly OR 包装在这里**不适用**——会导致方法返回叶子节点。正确的修复是在调用点剥离内容级过滤器（如 `category`），因为目录节点的 `category=""` 永远不匹配 category 过滤器：

```python
# 在调用点（约第 294 行），metadata_filter 传 None：
# 目录节点不携带 category 元数据，内容级过滤器无意义。
# 租户/用户范围过滤器已通过 scope_filter 单独应用。
global_results = await self._global_vector_search(
    collection=collection,
    query_vector=query_vector,
    sparse_query_vector=sparse_query_vector,
    limit=self.GLOBAL_SEARCH_TOPK,
    filter=None,  # 目录搜索时剥离内容级过滤器
    text_query=text_query,
)
```

Phase 1 任务：验证现有 4 处修复正常工作，修复 `_global_vector_search` 调用点，重跑 LoCoMo benchmark。

#### 4.5.3 Intent Router 优化

三项优化，灵感来自 OpenViking 的 IntentAnalyzer：

**A. Session 感知路由（来自 OpenViking）**

首要性能杠杆。OpenViking 的核心洞察：仅在存在 session 上下文时才调用 LLM。

```
有 session 上下文（摘要 + 最近消息）？
  ├─ 有 → LLM IntentAnalyzer → 多查询
  └─ 无 → 零 LLM 直接构造查询
```

对于直接的 API `search()` 调用（无 session），立即构造默认 `TypedQuery`——零 LLM 成本。这一项单独就能消除大多数搜索调用的 8-22s 延迟。

**B. 多查询并发检索（来自 OpenViking）**

当 LLM 分析被触发时（存在 session 上下文），生成多条不同角度的 `TypedQuery`：

```python
# 当前：单条查询
query → IntentRouter → 单次 search()

# 新方案：多查询（OpenViking 模式）
query + session_context → IntentAnalyzer → [TypedQuery_1, TypedQuery_2, TypedQuery_3]
  → asyncio.gather(search(tq1), search(tq2), search(tq3))
  → 合并 + 去重结果
```

每条 TypedQuery 可以针对不同的 `context_type`（memory/resource）并使用不同的查询改写。这对模糊查询的召回率提升显著。

**C. LRU 意图缓存**

缓存近期的意图分析结果（TTL 60s，maxsize 128）。短时间窗口内的相似查询复用缓存的 QueryPlan，无需重新调用 LLM。

## 5. 实施分期

### Phase 1：基础（无外部依赖）

- [ ] IngestModeResolver（orchestrator 中的路由逻辑）
- [ ] 向量化文本扩展：在 orchestrator `add()` 流程中，`_derive_layers()` 之后、`embed()` 之前设置 `Vectorize(abstract + keywords)`
- [ ] 验证现有 HierarchicalRetriever 目录过滤修复 + 修复 `_global_vector_search`
- [ ] 去重默认值改为关闭（`add()` 参数默认值变更）

**验证**：重跑 LoCoMo benchmark——期望非零检索命中。

### Phase 2：Conversation 模式（依赖 P1）

- [ ] 即时层：`add_message` 中逐条消息 embed + 写入
- [ ] 合并层：token 阈值触发、LLM 推导、替换即时记录
- [ ] Session 结束：刷新缓冲区、创建父节点、Alpha pipeline
- [ ] Observer 中的缓冲区管理（token 计数、合并触发）

**验证**：多轮对话 → 搜索特定消息内容 → 命中。

### Phase 3：Document 模式（依赖 P1，可与 P2 并行）

- [ ] 移植解析器基础类（`ParsedChunk`、`BaseParser`、`ParserConfig`）
- [ ] 移植解析器：Markdown、Text、Word、Excel、PowerPoint、PDF、EPUB
- [ ] 移植 CodeRepositoryParser + constants + upload_utils
- [ ] `ParserRegistry` 基于扩展名的调度
- [ ] `batch_add` 增强：从 `meta.file_path` 构建目录树
- [ ] batch_add 中的大文件分块
- [ ] 在 `pyproject.toml` 中添加可选依赖：`python-docx`、`openpyxl`、`python-pptx`、`pdfplumber`、`ebooklib`

**验证**：导入 PDF/docx → 搜索特定段落内容 → 命中且层级结构正确。

### Phase 4：Intent Router 优化（依赖 P1，可与 P2/P3 并行）

- [ ] Session 感知路由（无 session 时跳过 LLM——首要性能杠杆）
- [ ] 多查询并发检索（TypedQuery + asyncio.gather）
- [ ] LRU 意图缓存（TTL 60s，maxsize 128）
- [ ] Prompt 精简（减少输出字段）

**验证**：测量平均检索延迟——非 session 查询预期 < 2s（零 LLM）。

## 6. 关键文件

| 文件 | 操作 |
|------|------|
| `src/opencortex/orchestrator.py` | 添加 IngestModeResolver，修改 `add()`，增强 `batch_add()` |
| `src/opencortex/orchestrator.py`（add 流程） | `_derive_layers()` 之后设置 `Vectorize(abstract + keywords)`，在 `embed()` 之前 |
| `src/opencortex/retrieve/hierarchical_retriever.py` | 验证目录过滤修复（4 处），修复 `_global_vector_search` |
| `src/opencortex/retrieve/intent_router.py` | Session 感知路由、多查询、LRU 缓存 |
| `src/opencortex/context/manager.py` | 在 `commit()` 中接入即时层，合并触发 |
| `src/opencortex/alpha/observer.py` | Token 缓冲区管理、合并触发回调 |
| `src/opencortex/parse/` | **新目录**——移植的解析器 |
| `src/opencortex/parse/base.py` | 新建——`ParsedChunk`、`format_table_to_markdown`、`lazy_import` |
| `src/opencortex/parse/registry.py` | 新建——`ParserRegistry` |
| `src/opencortex/parse/parsers/` | 新建——markdown、text、word、excel、pptx、pdf、epub、code |
| `src/opencortex/parse/parsers/constants.py` | 新建——从 OpenViking 移植 |
| `pyproject.toml` | 添加可选依赖：python-docx、openpyxl、python-pptx、pdfplumber、ebooklib |

## 7. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 合并层删除即时记录时检索正在进行 | 短暂窗口内结果过时 | 接受最终一致性；合并为异步后台任务 |
| LLM 推导延迟阻塞合并 | chunk 质量升级延迟 | 合并异步运行，即时记录在合并完成前继续服务 |
| 解析器移植引入 OpenViking 耦合 | 维护负担 | 通过 `ParsedChunk` 接口清洁解耦；VikingFS 替换为 CortexFS |
| 大型代码库产生数千个 chunk | Qdrant 存储压力，batch_add 变慢 | LLM 推导并发限制（semaphore），进度上报 |
| 多查询并发检索增加 Qdrant 负载 | 向量存储 QPS 增高 | Qdrant embedded 可良好应对；限制 3-5 条并发查询 |
| 意图缓存返回过时结果 | 搜索参数错误 | 短 TTL（60s）；缓存键包含完整查询文本 |

## 8. 成功指标

1. **LoCoMo QA 命中率**：从 0% 提升至 > 50% 检索命中。
2. **平均检索延迟**：从 8-22s 降至非 session 查询 < 2s（通过 session 感知路由实现零 LLM）。
3. **代码搜索准确性**：查询"认证中间件"→ 返回相关代码文件及正确的文件路径。
4. **层级导航**：查询"src/opencortex/ 下有什么"→ 返回目录摘要。
5. **逐条消息可检索**：活跃 session 内，`add_message` 后 1s 内最新消息可被检索。
