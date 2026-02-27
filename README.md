<p align="center">
  <h1 align="center">OpenCortex</h1>
  <p align="center">AI Agent 的记忆与上下文管理系统</p>
  <p align="center">
    <a href="#快速开始">快速开始</a> · <a href="#架构设计">架构</a> · <a href="#mcp-tools">MCP Tools</a> · <a href="#sona-自学习排序">SONA</a> · <a href="docs/architecture.md">详细文档</a>
  </p>
</p>

---

## 为什么需要 OpenCortex

大模型 Agent 的上下文窗口是有限的。每次对话结束，Agent 遗忘一切。

OpenCortex 让 Agent 拥有**持久记忆**——跨会话的、可检索的、会自我进化的记忆系统：

- 对话中学到的用户偏好，下次自动召回
- 踩过的坑和修复方案，再遇到时立刻给出建议
- 重要代码模式和架构决策，始终保持上下文

不是简单的 key-value 存储，而是一个**三层摘要 + 强化学习排序 + 语义精排**的完整记忆引擎。

---

## 核心能力

### 三层摘要 (L0 / L1 / L2)

```
L0 摘要  →  一句话描述，用于向量检索             ← 极低 Token
L1 概要  →  段落级概要，用于初步判断             ← 低 Token
L2 全文  →  完整内容，按需加载                   ← 高 Token
```

检索时先用 L0 向量匹配，按需逐层下探。**90% 的场景只需 L0**，极大节省 Token。

### SONA 自学习排序

强化学习驱动的记忆排序——高价值记忆自然上浮，低价值记忆自然衰减：

```
reinforced_score = similarity × (1 + α × reward) × decay_factor
```

正反馈 → 分数增强 → 下次优先召回。长期不用 → 自然衰减 → 腾出空间给新记忆。

### 三阶检索管线

```
Embedding 召回 (top 20)
       ↓
SONA 加权 (reinforced_score = similarity × reward × decay)
       ↓
Rerank 精排 (cross-encoder / LLM listwise scoring)
       ↓
Score Fusion: final = β × rerank + (1-β) × sona_score
       ↓
层级传播 + 收敛检测 → 返回 Top K
```

### 上下文自迭代

会话结束时，系统自动分析对话内容，提取持久记忆：

```
Session End → LLM 分析 → 提取记忆 → 语义去重 (≥0.85 合并) → 存入 Viking FS
```

无需手动整理，Agent 的知识库自动增长。

### 租户级隔离

```
opencortex://tenant/{team}/user/{uid}/{type}/{category}/{node_id}
```

多团队、多用户，URI 命名空间完全隔离。团队级资源共享，用户级记忆私有。

### MCP Server

通过 [FastMCP v3](https://github.com/jlowin/fastmcp) 暴露 16 个工具，支持 stdio / SSE / streamable-http 三种传输模式。任何支持 MCP 协议的 Agent 均可接入。

---

## 架构设计

```
 Claude Code / Cursor / 自定义 Agent
          │
          │  MCP Protocol (stdio / SSE / streamable-http)
          ▼
┌───────────────────────────────────────────────────────────────┐
│                    FastMCP Server                              │
│  16 tools: memory_* / session_* / hooks_*                     │
├───────────────────────────────────────────────────────────────┤
│                    MemoryOrchestrator                          │
│  统一 API: add / search / feedback / decay / session / hooks  │
├──────────┬──────────────┬──────────────┬──────────────────────┤
│ Embedder │ IntentAnalyzer│ RerankClient │  SessionManager     │
│ (可插拔)  │ (LLM 意图)    │ (API/LLM/off)│ (生命周期+提取)     │
├──────────┴──────┬───────┴──────────────┴──────────────────────┤
│                 │                                              │
│   VikingFS      │     HierarchicalRetriever                   │
│  L0/L1/L2       │   三阶管线: Embedding → SONA → Rerank       │
│  三层文件系统    │   层级递归 + 分数传播 + 收敛检测              │
├─────────────────┴─────────────────────────────────────────────┤
│              VikingDBInterface (25 async methods)              │
├──────────────────────────┬────────────────────────────────────┤
│    RuVectorAdapter       │      InMemoryStorage (测试)         │
│  Standard: 25 方法        │                                    │
│  SONA: reward/decay/     │                                    │
│        profile/protect   │                                    │
├──────────────────────────┴────────────────────────────────────┤
│    RuVector (CLI / HTTP)  ←  HNSW 向量索引 + SONA 强化层      │
└───────────────────────────────────────────────────────────────┘
```

### 关键设计

**双面适配器 (Dual-Faced Adapter)**
RuVectorAdapter 同时实现 VikingDBInterface 标准面 (25 async 方法) 和 SONA 强化面 (reward / profile / decay / protect)。Orchestrator 通过 `hasattr` 检测是否支持 SONA，做到对存储后端零侵入。

**可插拔嵌入层**
```
EmbedderBase (ABC)
  ├── DenseEmbedderBase        → dense_vector (List[float])
  ├── SparseEmbedderBase       → sparse_vector (Dict[str, float])
  ├── HybridEmbedderBase       → dense + sparse
  └── CompositeHybridEmbedder  → 组合任意 Dense + Sparse
```

**Rerank 三模式降级**
1. **API 模式** — 专用 Rerank API (Volcengine / Jina / Cohere)
2. **LLM 模式** — 用 LLM completion 做 listwise rerank (降级方案)
3. **Disabled** — 纯 SONA + embedding，零额外开销

**层级递归检索**
```
全局向量搜索 → 定位候选目录
     ↓
递归深度遍历 (parent_uri 关系)
     ↓
每层: embedding 召回 → SONA 加权 → rerank 精排
     ↓
分数传播: final = α × child_score + (1-α) × parent_score
     ↓
收敛检测: top-K 连续 3 轮不变 → 停止
```

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
pip install -e .
# 或使用 uv
uv pip install -e .
```

### 2. 配置

创建 `opencortex.json`：

```json
{
  "tenant_id": "my-team",
  "user_id": "my-name",
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision-250615",
  "embedding_api_key": "YOUR_API_KEY",
  "embedding_api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "ruvector_host": "127.0.0.1",
  "ruvector_port": 6921,
  "mcp_transport": "streamable-http",
  "mcp_port": 8920
}
```

### 3. 启动 MCP Server

```bash
# Streamable HTTP 模式 (推荐，支持 Hooks 集成)
./scripts/start-mcp.sh

# stdio 模式 (Claude Desktop 本地连接)
PYTHONPATH=src python -m opencortex.mcp_server --transport stdio

# 后台启动
./scripts/start-mcp.sh --background
./scripts/start-mcp.sh --stop  # 停止
```

### 4. Claude Code 集成

**方式一：克隆即用（推荐）**

```bash
cd OpenCortex
# Claude Code 自动发现:
#   .mcp.json           → MCP Server (16 个工具)
#   .claude/settings.json → Hooks (自动记忆采集)
```

**方式二：仅 MCP Server**

在任意项目的 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "opencortex": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8920/mcp"
    }
  }
}
```

---

## MCP Tools

### 核心记忆工具

| Tool | 说明 |
|------|------|
| `memory_store` | 存储新记忆（自动 embedding + URI 生成 + L0/L1/L2 写入） |
| `memory_search` | 三阶语义搜索（embedding 召回 → SONA 加权 → rerank 精排） |
| `memory_feedback` | SONA 正/负反馈（正值增强召回优先级，负值抑制） |
| `memory_stats` | 存储统计 + rerank 状态 + SONA 配置 |
| `memory_decay` | 触发全局时间衰减（普通 0.95，受保护 0.99） |
| `memory_health` | 组件健康检查（storage / embedder / LLM / hooks） |

### 会话管理工具

| Tool | 说明 |
|------|------|
| `session_begin` | 开始新会话，缓冲消息用于结束时提取记忆 |
| `session_message` | 向活跃会话添加消息 |
| `session_end` | 结束会话 → LLM 分析 → 提取记忆 → 语义去重 → 自动存储 |

### Hooks 集成工具

| Tool | 说明 |
|------|------|
| `hooks_route` | 基于学习模式将任务路由到最佳 Agent |
| `hooks_init` | 初始化项目 hooks 配置 |
| `hooks_pretrain` | 从仓库内容预训练 |
| `hooks_verify` | 验证 hooks 配置 |
| `hooks_doctor` | 系统诊断（storage / embedder / LLM / hooks） |
| `hooks_export` | 导出智能数据（学习模式 / 记忆 / 轨迹） |
| `hooks_build_agents` | 基于学习模式生成 Agent 配置 |

---

## SONA 自学习排序

### 工作原理

```
                 ┌──────────────┐
                 │  Agent 交互   │
                 └──────┬───────┘
                        │
              ┌─────────▼─────────┐
              │  memory_feedback   │
              │  uri + reward      │
              └─────────┬─────────┘
                        │
          ┌─────────────▼──────────────┐
          │  RuVector SONA Engine       │
          │                             │
          │  reward_score += α × reward │
          │  retrieval_count++          │
          │  positive/negative_count++  │
          └─────────────┬──────────────┘
                        │
         ┌──────────────▼───────────────┐
         │  下次检索时                    │
         │  reinforced_score =           │
         │    similarity                 │
         │    × (1 + α × reward)         │
         │    × decay_factor             │
         └──────────────┬───────────────┘
                        │
           ┌────────────▼────────────┐
           │  高价值记忆上浮           │
           │  低价值记忆自然衰减       │
           └─────────────────────────┘
```

### API

```python
# 正反馈 → 增强召回优先级
await orch.feedback(uri="opencortex://...", reward=1.0)

# 负反馈 → 降低优先级
await orch.feedback(uri="opencortex://...", reward=-0.5)

# 时间衰减
await orch.decay()

# 保护重要记忆（衰减率 0.99 vs 普通 0.95）
await orch.protect(uri="opencortex://...", protected=True)

# 查看 SONA 行为画像
profile = await orch.get_profile(uri="opencortex://...")
# → reward_score, retrieval_count, positive/negative_feedback_count,
#   effective_score, is_protected, last_retrieved_at
```

---

## 上下文自迭代

### 流程

```
SessionStart Hook
     │
     ▼
session_begin(session_id)
     │
     ▼  ← 对话进行中，消息自动缓冲
     │
Stop Hook
     │
     ▼
session_end(session_id, quality_score)
     │
     ├─→ MemoryExtractor (LLM 分析对话)
     │     ├─ 提取 preferences (用户偏好)
     │     ├─ 提取 patterns (代码模式)
     │     ├─ 提取 entities (重要配置)
     │     ├─ 提取 skills (Agent 技能)
     │     └─ 提取 errors (错误方案)
     │
     ├─→ 语义去重 (score ≥ 0.85 → 合并更新)
     │
     └─→ Viking FS 写入 (L0/L1/L2 三层)
```

每次会话结束，Agent 的知识库自动增长。下次对话自动召回相关记忆。

---

## 项目结构

```
src/opencortex/
├── config.py                      # CortexConfig 全局配置
├── orchestrator.py                # MemoryOrchestrator 顶层编排 (1500+ 行)
├── mcp_server.py                  # FastMCP Server (16 tools)
│
├── core/                          # 核心数据模型
│   ├── context.py                 # Context 统一上下文 (L0/L1/L2)
│   ├── message.py                 # Message
│   └── user_id.py                 # UserIdentifier 租户隔离
│
├── models/                        # 模型层
│   ├── embedder/
│   │   ├── base.py                # EmbedderBase / Dense / Sparse / Hybrid
│   │   └── volcengine_embedders.py # 火山引擎 doubao-embedding-vision
│   └── llm_factory.py             # LLM completion 工厂 (Ark / OpenAI)
│
├── retrieve/                      # 检索层
│   ├── hierarchical_retriever.py  # 三阶管线: Embedding → SONA → Rerank
│   ├── intent_analyzer.py         # LLM 意图分析 → QueryPlan
│   ├── rerank_client.py           # RerankClient (API / LLM / disabled)
│   ├── rerank_config.py           # RerankConfig
│   └── types.py                   # TypedQuery / FindResult / ThinkingTrace
│
├── session/                       # 会话管理
│   ├── manager.py                 # SessionManager (begin/message/end)
│   ├── extractor.py               # MemoryExtractor (LLM 驱动)
│   └── types.py                   # SessionContext / ExtractedMemory
│
├── storage/                       # 存储层
│   ├── vikingdb_interface.py      # 抽象接口 (25 async methods)
│   ├── viking_fs.py               # VikingFS 三层文件系统
│   ├── collection_schemas.py      # 集合 Schema
│   └── ruvector/                  # RuVector 后端
│       ├── adapter.py             # 双面适配器 (Standard + SONA)
│       ├── cli_client.py          # CLI subprocess 封装
│       ├── http_client.py         # HTTP 客户端
│       ├── hooks_client.py        # 原生自学习 Hooks
│       ├── filter_translator.py   # 过滤 DSL 翻译
│       └── types.py               # RuVectorConfig / SonaProfile
│
└── utils/
    ├── uri.py                     # CortexURI 租户隔离 URI 体系
    └── time_utils.py              # 时间工具

scripts/
├── mcp-call.py                    # 轻量 MCP HTTP 客户端 (供 Hooks 调用)
└── start-mcp.sh                   # MCP Server 启动脚本

tests/
├── test_e2e_phase1.py             # 24 个 E2E 测试
├── test_mcp_server.py             # 8 个 MCP 测试
└── test_real_integration.py       # 真实集成测试
```

---

## Python API

```python
from opencortex import MemoryOrchestrator, CortexConfig, init_config

# 初始化
init_config(CortexConfig(tenant_id="myteam", user_id="alice"))
orch = MemoryOrchestrator(embedder=my_embedder)
await orch.init()

# 存储
ctx = await orch.add(
    abstract="用户偏好暗色主题",
    content="所有编辑器和终端使用暗色主题",
    category="preferences",
)

# 搜索 (三阶管线: embedding → SONA → rerank)
result = await orch.search("用户喜欢什么主题？")
for m in result.memories:
    print(f"{m.uri}: {m.abstract} (score={m.score:.3f})")

# 会话感知搜索 (LLM 意图分析 → 多查询)
result = await orch.session_search(
    query="帮我配置编辑器",
    messages=[Message(role="user", content="...")],
)

# 反馈 + 衰减
await orch.feedback(uri=ctx.uri, reward=1.0)
await orch.decay()

# 会话自迭代
await orch.session_begin(session_id="s1")
await orch.session_message("s1", "user", "帮我修复这个 bug")
await orch.session_message("s1", "assistant", "问题是...")
result = await orch.session_end("s1", quality_score=0.9)
# → 自动提取并存储记忆

await orch.close()
```

### 完整方法列表

| 类别 | 方法 | 说明 |
|------|------|------|
| 生命周期 | `init()` / `close()` | 初始化 / 关闭 |
| | `health_check()` / `stats()` | 健康检查 / 统计 (含 rerank 状态) |
| CRUD | `add()` / `update()` / `remove()` | 增 / 改 / 删 |
| 检索 | `search()` | 三阶管线检索 |
| | `session_search()` | 会话感知检索 (需 LLM) |
| SONA | `feedback()` / `feedback_batch()` | 正/负反馈 |
| | `decay()` | 时间衰减 |
| | `protect()` / `get_profile()` | 保护记忆 / 查看画像 |
| 会话 | `session_begin()` / `session_message()` / `session_end()` | 生命周期 + 自迭代 |
| Hooks | `hooks_route()` / `hooks_doctor()` / `hooks_export()` ... | 集成管理 |

---

## 运行测试

```bash
# 全部 32 个测试
PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server -v

# E2E 测试 (24 个)
PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 -v

# MCP 测试 (8 个)
PYTHONPATH=src python3 -m unittest tests.test_mcp_server -v
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.10+, async-first |
| 向量存储 | RuVector (HNSW + SONA 强化层) |
| Embedding | 火山引擎 doubao-embedding-vision (1024 dim, 支持多模态) |
| Rerank | Volcengine / Jina / Cohere API 或 LLM fallback |
| LLM | 火山引擎 Ark SDK (doubao-seed) / OpenAI compatible |
| MCP | PrefectHQ FastMCP v3 |
| 测试 | unittest (32 用例全通过) |

---

## Hooks 自动记忆采集

项目内置 Claude Code Hooks（`.claude/settings.json`），通过 MCP Server 路由：

```
Hooks → scripts/mcp-call.py → HTTP → MCP Server → Orchestrator → Viking FS → RuVector
```

| Hook | 触发时机 | 行为 |
|------|---------|------|
| **PreToolUse** | Edit/Write/Bash/Read/Glob 前 | 搜索相关记忆，提供上下文 |
| **PostToolUse** | Edit/Write/Bash 后 | 记录操作到记忆库 |
| **SessionStart** | 会话启动 | `session_begin` 开始缓冲 |
| **Stop** | 会话结束 | `session_end` 触发自迭代 |

所有 Hook 通过 MCP Server 执行，经过完整的 Viking FS 三层摘要和 SONA 强化学习管线。

---

## License

[Apache-2.0](LICENSE)

---

## 致谢

OpenCortex 从以下开源项目移植并重构，在此致以诚挚感谢：

### [OpenViking](https://github.com/volcengine/openviking)

火山引擎开源的 AI Agent 上下文管理框架。OpenCortex 的核心架构直接源自 OpenViking：

- **VikingFS 三层文件系统** — L0/L1/L2 摘要体系的原始设计
- **层级递归检索算法** — HierarchicalRetriever 的分数传播与收敛检测机制
- **VikingDBInterface** — 25 个 async 方法的存储抽象接口
- **IntentAnalyzer** — LLM 驱动的会话意图分析与查询规划

### [RuVector](https://github.com/nicholasgasior/ruvector)

高性能 Rust 向量数据库引擎。OpenCortex 的向量存储与强化学习层基于 RuVector 构建：

- **HNSW 向量索引** — 高效近似最近邻搜索
- **SONA 强化学习引擎** — reward / decay / profile / protect 四方法的底层实现
- **RuVector Hooks** — Q-learning + 轨迹追踪 + 错误模式学习的原生自学习能力
- **CLI / HTTP 双协议** — 灵活的进程间通信方式

感谢这两个项目的作者和贡献者，OpenCortex 站在巨人的肩膀上。
