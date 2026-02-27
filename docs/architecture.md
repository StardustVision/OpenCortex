# OpenCortex 架构文档

> 版本：v0.2.0 | 更新日期：2026-02-27

## 1. 系统概述

OpenCortex 是面向 AI Agent 的**记忆与上下文管理系统**，核心目标：

- **节省 Token**：三层摘要体系（L0/L1/L2），检索时只返回必要的精度层级
- **自学习 Memory**：通过强化学习排序，让高价值记忆自然上浮，低价值记忆自然衰减
- **即插即用**：Claude Code 插件一键安装，自动采集对话记忆 + 自动召回

项目从 [OpenViking](https://github.com/volcengine/openviking) 移植并重构，新增**Qdrant 嵌入式存储**、**HTTP Server**、**RL 分数融合**和**Claude Code 插件系统**。

---

## 2. 系统架构

### 2.1 全局架构

```
 Claude Code / Cursor / 自定义 Agent
          │
          ├─── Hook 自动触发 ─────────────────────┐
          │    SessionStart / UserPromptSubmit      │
          │    Stop / SubagentStop                  │
          │                                         ▼
          │                              ┌──────────────────────┐
          │                              │  opencortex-memory    │
          │                              │  Plugin               │
          │                              │  hooks/ → oc_memory.py│
          │                              │  (urllib HTTP client) │
          │                              └──────────┬───────────┘
          │                                         │
          │  MCP Protocol                           │ HTTP (urllib)
          │  (stdio/sse/streamable-http)            │
          ▼                                         ▼
┌──────────────────────────────────────────────────────────────┐
│                FastMCP Server (:8920/mcp)                     │
│  16 tools: memory_* / session_* / hooks_*                    │
│  mode: remote → forwards to HTTP Server                       │
│  mode: local → embedded Orchestrator                          │
├──────────────────────────────────────────────────────────────┤
│                FastAPI HTTP Server (:8921)                     │
│  REST API: /api/v1/memory/* /session/* /hooks/* /integration/*│
├──────────────────────────────────────────────────────────────┤
│                   MemoryOrchestrator                          │
│  统一 API: add / search / feedback / decay / session / hooks  │
├──────────┬──────────────┬──────────────┬─────────────────────┤
│ Embedder │ IntentAnalyzer│ RerankClient │  SessionManager     │
│ (可插拔)  │ (LLM 意图)    │ (API/LLM/off)│ (生命周期+提取)     │
├──────────┴──────┬───────┴──────────────┴─────────────────────┤
│                 │                                              │
│   VikingFS      │     HierarchicalRetriever                   │
│  L0/L1/L2       │   Embedding → Rerank → RL Fusion            │
│  三层文件系统    │   层级递归 + 分数传播 + 收敛检测              │
├─────────────────┴────────────────────────────────────────────┤
│              VikingDBInterface (25 async methods)              │
├──────────────────────────┬───────────────────────────────────┤
│  QdrantStorageAdapter    │      InMemoryStorage (测试)        │
│  Standard: 25 方法        │                                    │
│  RL: update_reward /      │                                    │
│      get_profile /        │                                    │
│      apply_decay /        │                                    │
│      set_protected        │                                    │
├──────────────────────────┴───────────────────────────────────┤
│  Qdrant (嵌入式)  ←  零外部进程，单进程内存模式                 │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 调用链

```
Agent → Hook/MCP/HTTP → Orchestrator → VikingFS → Qdrant
                                      → HierarchicalRetriever → Embedder + Reranker
```

### 2.3 双模式部署

| 模式 | Hook 行为 | 服务管理 | 适用场景 |
|------|----------|---------|---------|
| **Local** | SessionStart 自动启动 HTTP+MCP | PID 文件管理 | 个人开发 |
| **Remote** | 验证远程 HTTP 连接性 | 外部管理 | 团队共享 |

---

## 3. 核心组件

### 3.1 HTTP Server (FastAPI)

HTTP Server 是系统的**单一入口 (Single Source of Truth)**：

- 管理唯一的 Orchestrator 实例（lifespan 管理初始化/关闭）
- 所有存储操作经过同一个 Qdrant 连接
- Plugin hooks、MCP Server (remote 模式)、外部客户端均通过 HTTP 调用

```python
# server.py lifespan
@asynccontextmanager
async def _lifespan(app: FastAPI):
    _orchestrator = MemoryOrchestrator(config=config)
    await _orchestrator.init()
    yield
    await _orchestrator.close()
```

### 3.2 MCP Server (FastMCP v3)

MCP Server 支持两种运行模式：

- **remote** (默认)：MCP 作为薄客户端，转发到 HTTP Server
- **local**：MCP 内嵌 Orchestrator，适合开发调试

支持 stdio / SSE / streamable-http 三种传输协议。

### 3.3 MemoryOrchestrator

顶层编排器 (1500+ 行)，统一管理所有记忆操作：

| 类别 | 方法 | 说明 |
|------|------|------|
| 生命周期 | `init()` / `close()` / `health_check()` / `stats()` | 初始化与监控 |
| CRUD | `add()` / `update()` / `remove()` | 记忆增删改 |
| 检索 | `search()` / `session_search()` | 三阶检索 / 会话感知检索 |
| RL | `feedback()` / `decay()` / `protect()` / `get_profile()` | 强化学习 |
| 会话 | `session_begin()` / `session_message()` / `session_end()` | 会话自迭代 |
| Hooks | `hooks_learn()` / `hooks_remember()` / `hooks_recall()` ... | 集成管理 |

### 3.4 VikingFS 三层文件系统

| 层级 | 文件 | 用途 | Token 消耗 |
|------|------|------|-----------|
| L0 | `.abstract.md` + 向量库 | 一句话描述，用于向量检索 | 极低 |
| L1 | `.overview.md` | 段落级概要，用于初步判断 | 低 |
| L2 | `content.md` | 完整内容，按需加载 | 高 |

### 3.5 HierarchicalRetriever

三阶检索管线 + RL 分数融合：

```
1. 全局向量搜索 → 定位候选目录
2. 合并起始点（根目录 + 全局命中）
3. 递归搜索：按 parent_uri 深度遍历
   - Embedding 召回
   - Rerank 精排 (API / LLM / disabled)
   - RL Fusion: fused += rl_weight × reward_score
   - 分数传播: final = α × child + (1-α) × parent
   - 收敛检测: top-K 连续 3 轮不变 → 停止
4. 返回 Top K MatchedContext
```

**RL 融合参数**：
- `rl_weight = 0.05`（保守值）
- reward_score=1.0 → +0.05 分（向量 score 通常 0.3~0.6）
- 负向 reward 同样起作用：reward=-2 → -0.10 分

### 3.6 QdrantStorageAdapter

双面适配器设计：

**标准面 (VikingDBInterface)**：25 个 async 方法
- create_collection / insert / search / filter / scroll / count ...

**RL 强化面** (通过 `hasattr` 检测)：
- `update_reward(collection, id, reward)` — 累加 reward_score，更新 pos/neg 计数
- `get_profile(collection, id)` → Profile — 返回 RL 行为画像
- `set_protected(collection, id, protected)` — 标记保护状态
- `apply_decay(decay_rate=0.95, protected_rate=0.99, threshold=0.01)` → DecayResult

**RL 字段** (Qdrant payload)：

| 字段 | 类型 | 说明 |
|------|------|------|
| `reward_score` | float | 累计奖励分数 |
| `positive_feedback_count` | int64 | 正反馈次数 |
| `negative_feedback_count` | int64 | 负反馈次数 |
| `protected` | bool | 受保护标记 |

---

## 4. 插件系统

### 4.1 Plugin 结构

```
plugins/opencortex-memory/
├── .claude-plugin/plugin.json     # 插件清单
├── config.json                    # local/remote 模式配置
├── install.sh                     # 注册 Hooks 到 .claude/settings.json
├── uninstall.sh                   # 移除 Hooks + 清理状态
├── hooks/
│   ├── common.sh                  # 共享工具 (_json_val, get_http_url, run_bridge...)
│   ├── session-start.sh           # SessionStart: 启动服务 / 验证连接
│   ├── user-prompt-submit.sh      # UserPromptSubmit: 自动召回
│   ├── stop.sh                    # Stop: 后台摘要+存储
│   └── session-end.sh             # 手动: 存储摘要 + 关闭服务
├── scripts/
│   └── oc_memory.py               # HTTP client bridge (urllib, 零外部依赖)
└── skills/
    ├── memory-recall/SKILL.md     # /memory-recall — 搜索记忆
    ├── memory-store/SKILL.md      # /memory-store — 存储记忆
    ├── memory-feedback/SKILL.md   # /memory-feedback — RL 反馈
    ├── memory-stats/SKILL.md      # /memory-stats — 系统统计
    ├── memory-decay/SKILL.md      # /memory-decay — 奖励衰减
    └── memory-health/SKILL.md     # /memory-health — 健康检查
```

### 4.2 Hook 生命周期

```
SessionStart (会话开始，一次性)
  │  Local: nohup python -m opencortex.http → HTTP Server PID
  │  Local: nohup python -m opencortex.mcp_server → MCP Server PID
  │  Remote: curl health check
  │  → 写入 session_state.json (PIDs, http_url, tenant, user)
  ▼
UserPromptSubmit (每次用户输入)
  │  → oc_memory.py recall --query "$PROMPT"
  │  → POST /api/v1/memory/search
  │  → systemMessage 注入模型上下文
  ▼
Stop (每次 Agent 响应完成)
  │  → oc_memory.py ingest-stop --transcript-path ...
  │  → 解析 transcript JSONL 最后一轮
  │  → Claude Haiku 生成摘要 (fallback: 截断)
  │  → POST /api/v1/memory/store
  │  → (后台执行，不阻塞 Claude Code)
  ▼
SessionEnd (手动调用 session-end.sh)
  │  → POST session summary → /api/v1/memory/store
  │  → kill HTTP_PID + MCP_PID (local 模式)
  └  → 标记 session state inactive
```

### 4.3 Bridge (oc_memory.py)

HTTP client bridge，使用 stdlib `urllib.request`，**零外部依赖**：

| 命令 | HTTP 调用 | 说明 |
|------|----------|------|
| `recall --query Q` | POST /api/v1/memory/search | 搜索记忆，格式化输出 |
| `ingest-stop --transcript-path P` | POST /api/v1/memory/store | 解析transcript + 摘要 + 存储 |
| `session-end` | POST /api/v1/memory/store | 存储 session summary |
| `session-start` | (无 HTTP 调用) | 验证配置 + 写入初始状态 |

### 4.4 Skills (被动 vs 主动)

| 层 | 机制 | 触发方式 | 代表 |
|----|------|---------|------|
| **被动记忆** | Hooks | 自动 (每轮对话) | stop.sh → ingest, user-prompt-submit.sh → recall |
| **主动记忆** | Skills | Agent 按需 | /memory-store, /memory-feedback, /memory-decay |
| **外部访问** | MCP Tools | MCP 协议 | memory_store, memory_search, memory_feedback |
| **REST API** | HTTP | HTTP 客户端 | POST /api/v1/memory/* |

---

## 5. 存储后端

### 5.1 VikingDBInterface

抽象接口，定义 25 个 async 方法：

| 类别 | 方法数 | 说明 |
|------|--------|------|
| 集合管理 | 5 | create/drop/exists/list/info |
| 单条 CRUD | 6 | insert/update/upsert/delete/get/exists |
| 批量 CRUD | 4 | batch_insert/batch_upsert/batch_delete/remove_by_uri |
| 搜索 | 3 | search/filter/scroll |
| 聚合 | 1 | count |
| 索引 | 2 | create_index/drop_index |
| 生命周期 | 5 | clear/optimize/close/health_check/get_stats |

### 5.2 Qdrant 嵌入式存储

使用 `qdrant-client` 的嵌入式模式（`:memory:` 或本地路径），**零外部进程**：

```python
from qdrant_client import QdrantClient

# 内存模式 (测试)
client = QdrantClient(":memory:")

# 持久化模式 (生产)
client = QdrantClient(path="./data/qdrant")
```

**VikingDB DSL → Qdrant Filter 翻译**：

支持 `must / must_not / should` 条件，嵌套布尔逻辑，自动翻译为 Qdrant Filter 对象。

---

## 6. 嵌入模型

### 6.1 抽象层级

```
EmbedderBase (ABC)
  ├── DenseEmbedderBase       → List[float]
  ├── SparseEmbedderBase      → Dict[str, float]
  ├── HybridEmbedderBase      → dense + sparse
  └── CompositeHybridEmbedder → 组合任意 Dense + Sparse
```

### 6.2 支持的嵌入模型

| Provider | 模型 | 维度 | 说明 |
|----------|------|------|------|
| Volcengine | doubao-embedding-vision-250615 | 1024 | 多模态，支持图文 |
| OpenAI | text-embedding-3-small/large | 1536/3072 | 通用文本嵌入 |
| OpenAI Compatible | 任意 /v1/embeddings 兼容模型 | 可配置 | 第三方兼容服务 |

---

## 7. 配置

### 7.1 opencortex.json

```json
{
  "tenant_id": "my-team",
  "user_id": "my-name",
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision-250615",
  "embedding_api_key": "YOUR_API_KEY",
  "embedding_api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "embedding_dimension": 1024,
  "http_server_host": "127.0.0.1",
  "http_server_port": 8921,
  "mcp_transport": "streamable-http",
  "mcp_port": 8920,
  "rerank_mode": "api",
  "rerank_model": "doubao-seed-1-8-251228"
}
```

### 7.2 Plugin config.json

```json
{
  "mode": "local",
  "local": {
    "http_port": 8921,
    "mcp_port": 8920,
    "mcp_transport": "streamable-http",
    "data_dir": "data/vector"
  },
  "remote": {
    "http_url": "http://your-server:8921",
    "mcp_url": "http://your-server:8920/mcp"
  }
}
```

---

## 8. 测试

### 8.1 测试矩阵

| 测试文件 | 用例数 | 依赖 | 说明 |
|----------|--------|------|------|
| test_e2e_phase1.py | 24 | InMemory | 核心管线 E2E |
| test_mcp_server.py | 8 | InMemory | MCP 工具注册与调用 |
| test_qdrant_adapter.py | N | Qdrant + API | Qdrant 适配器 |
| test_rl_integration.py | 8 | Qdrant + API | RL 全流程 |
| test_mcp_qdrant.py | 6 | Qdrant + API | MCP + 真实存储 |
| test_http_server.py | N | HTTP Server | HTTP API |
| test_live_servers.py | 16 | HTTP + MCP | 服务回归 |
| test_openai_models.py | N | OpenAI API | 嵌入模型 |
| test_ace_phase{1,2,3}.py | N | Qdrant + API | ACE 引擎 |

### 8.2 运行命令

```bash
# 核心测试 (零外部依赖)
PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server -v

# RL + Qdrant 测试
PYTHONPATH=src python3 -m unittest tests.test_rl_integration -v

# 全量回归
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## 9. 技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 向量存储 | Qdrant (嵌入式) | 零外部进程，单文件持久化，丰富的过滤语法 |
| HTTP Server | FastAPI | async-first，Pydantic 校验，自动 OpenAPI |
| MCP 框架 | FastMCP v3 | 比 mcp SDK 更成熟，decorator 注册 |
| Hook Bridge | urllib (stdlib) | 零依赖，shell 脚本可直接调用 |
| RL 分数融合 | 加法融合 (additive) | 保守、可解释、不破坏原有排序 |
| Plugin 架构 | Hook (被动) + Skill (主动) | 自动采集 + 按需交互，覆盖全场景 |
| 服务管理 | SessionStart/End 管理 PID | 无需 systemd/docker，开发者友好 |
