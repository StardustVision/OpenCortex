<p align="center">
  <h1 align="center">OpenCortex</h1>
  <p align="center">AI Agent 的记忆与上下文管理系统</p>
  <p align="center">
    <a href="#快速开始">快速开始</a> · <a href="#架构设计">架构</a> · <a href="#插件系统">插件</a> · <a href="#mcp-tools">MCP Tools</a> · <a href="#sona-自学习排序">SONA</a> · <a href="docs/architecture.md">详细文档</a>
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
fused_score = similarity + rl_weight × reward_score
```

正反馈 → 分数增强 → 下次优先召回。长期不用 → 自然衰减 → 腾出空间给新记忆。

### 三阶检索管线

```
Embedding 召回 (top 20)
       ↓
Rerank 精排 (cross-encoder / LLM listwise scoring)
       ↓
Score Fusion: final = β × rerank + (1-β) × retrieval + rl_weight × reward
       ↓
层级传播 + 收敛检测 → 返回 Top K
```

### 上下文自迭代

每次对话结束时，Stop Hook 自动提取对话记忆：

```
Stop Hook → 解析 Transcript → LLM 摘要 → POST HTTP Server → Qdrant 存储
```

无需手动整理，Agent 的知识库自动增长。

### 租户级隔离

```
opencortex://tenant/{team}/user/{uid}/{type}/{category}/{node_id}
```

多团队、多用户，URI 命名空间完全隔离。团队级资源共享，用户级记忆私有。

---

## 架构设计

```
 Claude Code / Cursor / 自定义 Agent
          │
          │  Hook 自动触发 (SessionStart / UserPromptSubmit / Stop)
          ▼
┌───────────────────────────────────────────────────────────────┐
│            opencortex-memory Plugin                           │
│  hooks/: session-start.sh | user-prompt-submit.sh | stop.sh  │
│  scripts/: oc_memory.py (HTTP client bridge)                  │
│  skills/: memory-recall | memory-store | memory-feedback ...  │
├───────────────────────────────────────────────────────────────┤
│  oc_memory.py → urllib HTTP calls                             │
│    ingest-stop → POST /api/v1/memory/store                    │
│    recall → POST /api/v1/memory/search                        │
│    session-end → POST /api/v1/memory/store                    │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│    ┌──────────────────────────────────────────────────┐       │
│    │          FastAPI HTTP Server (:8921)              │       │
│    │  REST endpoints: /api/v1/memory/* /session/* ...  │       │
│    ├──────────────────────────────────────────────────┤       │
│    │          FastMCP Server (:8920/mcp)               │       │
│    │  16 tools: memory_* / session_* / hooks_*         │       │
│    │  transport: streamable-http / sse / stdio          │       │
│    ├──────────────────────────────────────────────────┤       │
│    │          MemoryOrchestrator                       │       │
│    │  统一 API: add / search / feedback / decay / ...  │       │
│    ├──────┬───────────┬───────────┬──────────────────┤       │
│    │Embed │ IntentAnlz│ RerankCli │  SessionManager  │       │
│    │(可插拔)│ (LLM意图) │ (API/LLM) │ (生命周期+提取)  │       │
│    ├──────┴─────┬─────┴───────────┴──────────────────┤       │
│    │            │                                     │       │
│    │  VikingFS  │  HierarchicalRetriever              │       │
│    │  L0/L1/L2  │  Embedding → Rerank → RL Fusion     │       │
│    ├────────────┴─────────────────────────────────────┤       │
│    │         VikingDBInterface (25 async methods)      │       │
│    ├──────────────────┬───────────────────────────────┤       │
│    │ QdrantAdapter    │     InMemoryStorage (测试)      │       │
│    │ Standard: 25方法  │                                │       │
│    │ RL: reward/decay/ │                                │       │
│    │    profile/protect│                                │       │
│    ├──────────────────┴───────────────────────────────┤       │
│    │  Qdrant (嵌入式)  ←  零外部进程，单进程内存模式    │       │
│    └──────────────────────────────────────────────────┘       │
└───────────────────────────────────────────────────────────────┘
```

### 双模式部署

| 模式 | 说明 | 适用场景 |
|------|------|---------|
| **Local** (默认) | SessionStart 自动启动 HTTP + MCP 服务，SessionEnd 自动关闭 | 个人开发，单机使用 |
| **Remote** | 配置远程 HTTP 服务器地址，Hook 直接调用远程 API | 团队共享，服务器部署 |

```json
// plugins/opencortex-memory/config.json
{
  "mode": "local",
  "local": { "http_port": 8921, "mcp_port": 8920 },
  "remote": { "http_url": "http://your-server:8921" }
}
```

### 关键设计

**HTTP Server 为核心 (Single Source of Truth)**

所有记忆操作统一经过 HTTP Server → Orchestrator → Qdrant。Hook 脚本不直接导入 Python 模块，而是通过 urllib 发 HTTP 请求。这保证了：
- 单一 Orchestrator 实例管理所有状态
- Hook 脚本轻量、快速、无 Python 环境依赖冲突
- Local/Remote 模式切换只需改 URL

**双面适配器 (Dual-Faced Adapter)**

QdrantStorageAdapter 同时实现 VikingDBInterface 标准面 (25 async 方法) 和 RL 强化面 (update_reward / get_profile / apply_decay / set_protected)。Orchestrator 通过 `hasattr` 检测是否支持 RL，做到对存储后端零侵入。

**可插拔嵌入层**
```
EmbedderBase (ABC)
  ├── DenseEmbedderBase        → dense_vector (List[float])
  ├── SparseEmbedderBase       → sparse_vector (Dict[str, float])
  ├── HybridEmbedderBase       → dense + sparse
  └── CompositeHybridEmbedder  → 组合任意 Dense + Sparse
```

支持 Volcengine doubao-embedding、OpenAI text-embedding 等多种嵌入模型。

**Rerank 三模式降级**
1. **API 模式** — 专用 Rerank API (Volcengine / Jina / Cohere)
2. **LLM 模式** — 用 LLM completion 做 listwise rerank (降级方案)
3. **Disabled** — 纯 embedding + RL fusion，零额外开销

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
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
  "http_server_host": "127.0.0.1",
  "http_server_port": 8921,
  "mcp_transport": "streamable-http",
  "mcp_port": 8920
}
```

### 3. 安装 Claude Code 插件

```bash
bash plugins/opencortex-memory/install.sh
```

安装后，Claude Code 会话自动触发记忆采集与召回。卸载：

```bash
bash plugins/opencortex-memory/uninstall.sh
```

### 4. 手动启动服务 (可选)

插件在 Local 模式下会自动管理服务生命周期。如需手动启动：

```bash
# HTTP Server
PYTHONPATH=src python -m opencortex.http --config opencortex.json --port 8921

# MCP Server (streamable-http 模式，连接 HTTP Server)
PYTHONPATH=src python -m opencortex.mcp_server --config opencortex.json \
  --transport streamable-http --port 8920 --mode remote
```

### 5. Claude Code 集成 (其他项目)

**方式一：MCP 连接**

```bash
claude mcp add opencortex -s user -- python -m opencortex.mcp_server \
  --transport stdio --config ~/.opencortex/opencortex.json
```

**方式二：`.mcp.json`**

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

## 插件系统

### opencortex-memory Plugin

插件通过 Claude Code Hooks 实现**被动记忆**（自动采集 + 自动召回），通过 Skills 实现**主动记忆**（Agent 按需调用）。

```
plugins/opencortex-memory/
├── .claude-plugin/plugin.json  # 插件清单
├── config.json                 # 模式配置 (local/remote)
├── install.sh                  # 安装 (注册 Hooks)
├── uninstall.sh                # 卸载 (移除 Hooks + 清理状态)
├── hooks/
│   ├── common.sh               # 共享工具函数
│   ├── session-start.sh        # SessionStart: 启动 HTTP+MCP 服务
│   ├── user-prompt-submit.sh   # UserPromptSubmit: 自动召回记忆
│   ├── stop.sh                 # Stop: 自动摘要 + 存储当前对话轮
│   └── session-end.sh          # 手动调用: 存储摘要 + 关闭服务
├── scripts/
│   └── oc_memory.py            # HTTP client bridge
└── skills/
    ├── memory-recall/          # 搜索历史记忆
    ├── memory-store/           # 存储新记忆
    ├── memory-feedback/        # RL 反馈
    ├── memory-stats/           # 系统统计
    ├── memory-decay/           # 奖励衰减
    └── memory-health/          # 健康检查
```

### Hook 生命周期

```
SessionStart
  │  → 启动 HTTP Server + MCP Server (local 模式)
  │  → 验证远程连接 (remote 模式)
  │  → 写入 session_state.json
  ▼
UserPromptSubmit (每次用户输入)
  │  → 提取用户 prompt
  │  → POST /api/v1/memory/search (自动召回)
  │  → 注入 systemMessage 到模型上下文
  ▼
Stop (每次 Agent 响应完成)
  │  → 解析 Transcript 最后一轮
  │  → Claude Haiku 摘要
  │  → POST /api/v1/memory/store (后台执行)
  ▼
SessionEnd (手动/卸载时)
  │  → POST session summary
  │  → Kill HTTP + MCP PIDs
  └  → 标记 session inactive
```

### Skills

| Skill | 说明 | API |
|-------|------|-----|
| `memory-recall` | 搜索历史记忆 | POST /api/v1/memory/search |
| `memory-store` | 存储新记忆 | POST /api/v1/memory/store |
| `memory-feedback` | RL 正/负反馈 | POST /api/v1/memory/feedback |
| `memory-stats` | 系统统计 | GET /api/v1/memory/stats |
| `memory-decay` | 全局奖励衰减 | POST /api/v1/memory/decay |
| `memory-health` | 健康检查 | GET /api/v1/memory/health |

---

## MCP Tools

### 核心记忆工具

| Tool | 说明 |
|------|------|
| `memory_store` | 存储新记忆（自动 embedding + URI 生成 + L0/L1/L2 写入） |
| `memory_search` | 三阶语义搜索（embedding 召回 → rerank 精排 → RL fusion） |
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
| `hooks_learn` | 记录 state-action-reward 用于策略学习 |
| `hooks_remember` | 存储通用记忆 |
| `hooks_recall` | 检索相关经验 |
| `hooks_init` / `hooks_pretrain` | 初始化 + 预训练 |
| `hooks_verify` / `hooks_doctor` | 验证 + 诊断 |
| `hooks_export` / `hooks_build_agents` | 导出 + 生成 Agent 配置 |

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
          │  Qdrant RL Layer            │
          │  (QdrantStorageAdapter)     │
          │                             │
          │  update_reward:             │
          │    reward_score += reward   │
          │    positive_count++         │
          │    negative_count++         │
          │                             │
          │  get_profile → Profile:     │
          │    reward_score             │
          │    retrieval_count          │
          │    positive/negative_count  │
          │    effective_score          │
          │    is_protected             │
          └─────────────┬──────────────┘
                        │
         ┌──────────────▼───────────────┐
         │  HierarchicalRetriever       │
         │  Score Fusion:               │
         │                              │
         │  fused = β × rerank          │
         │        + (1-β) × retrieval   │
         │        + rl_weight × reward  │
         │                              │
         │  rl_weight = 0.05 (保守)     │
         │  reward=1 → +0.05 分         │
         │  reward=-2 → -0.10 分        │
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

# 时间衰减 (普通 0.95, 保护 0.99)
result = await orch.decay()
# → DecayResult(records_processed=60, records_decayed=12, ...)

# 保护重要记忆（衰减率降低）
await orch.protect(uri="opencortex://...", protected=True)

# 查看 SONA 行为画像
profile = await orch.get_profile(uri="opencortex://...")
# → Profile(reward_score=3.0, retrieval_count=5, is_protected=True, ...)
```

### Qdrant RL 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `reward_score` | float | 累计奖励分数 |
| `positive_feedback_count` | int64 | 正反馈次数 |
| `negative_feedback_count` | int64 | 负反馈次数 |
| `protected` | bool | 是否受保护（衰减更慢） |

---

## HTTP Server REST API

HTTP Server 是系统的核心入口，所有操作通过 REST API 暴露。

### 核心记忆

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/memory/store` | 存储记忆 |
| POST | `/api/v1/memory/search` | 语义搜索 |
| POST | `/api/v1/memory/feedback` | RL 反馈 |
| GET | `/api/v1/memory/stats` | 统计信息 |
| POST | `/api/v1/memory/decay` | 奖励衰减 |
| GET | `/api/v1/memory/health` | 健康检查 |

### 会话

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/session/begin` | 开始会话 |
| POST | `/api/v1/session/message` | 添加消息 |
| POST | `/api/v1/session/end` | 结束会话 + 提取记忆 |

### Hooks 集成

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/hooks/learn` | 记录学习事件 |
| POST | `/api/v1/hooks/remember` | 存储记忆 |
| POST | `/api/v1/hooks/recall` | 检索经验 |
| GET | `/api/v1/hooks/stats` | 学习统计 |
| POST | `/api/v1/hooks/trajectory/begin` | 开始轨迹 |
| POST | `/api/v1/hooks/trajectory/step` | 轨迹步骤 |
| POST | `/api/v1/hooks/trajectory/end` | 结束轨迹 |
| POST | `/api/v1/hooks/error/record` | 记录错误修复 |
| POST | `/api/v1/hooks/error/suggest` | 错误建议 |
| POST | `/api/v1/integration/route` | 任务路由 |
| POST | `/api/v1/integration/init` | 初始化 |
| GET | `/api/v1/integration/verify` | 验证 |
| GET | `/api/v1/integration/doctor` | 诊断 |
| POST | `/api/v1/integration/export` | 导出 |
| GET | `/api/v1/integration/build-agents` | 生成 Agent 配置 |

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

# 搜索 (三阶管线: embedding → rerank → RL fusion)
result = await orch.search("用户喜欢什么主题？")
for m in result.memories:
    print(f"{m.uri}: {m.abstract} (score={m.score:.3f})")

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

---

## 项目结构

```
src/opencortex/
├── config.py                      # CortexConfig 全局配置
├── orchestrator.py                # MemoryOrchestrator 顶层编排 (1500+ 行)
├── mcp_server.py                  # FastMCP Server (16 tools, 双模式)
│
├── http/                          # HTTP Server
│   ├── server.py                  # FastAPI 应用 + REST routes
│   ├── client.py                  # OpenCortexClient (异步 HTTP 客户端)
│   └── models.py                  # Pydantic 请求模型
│
├── core/                          # 核心数据模型
│   ├── context.py                 # Context 统一上下文 (L0/L1/L2)
│   ├── message.py                 # Message
│   └── user_id.py                 # UserIdentifier 租户隔离
│
├── models/                        # 模型层
│   ├── embedder/
│   │   ├── base.py                # EmbedderBase / Dense / Sparse / Hybrid
│   │   ├── volcengine_embedders.py # 火山引擎 doubao-embedding-vision
│   │   └── openai_embedder.py     # OpenAI compatible embedding
│   └── llm_factory.py             # LLM completion 工厂 (Ark / OpenAI)
│
├── retrieve/                      # 检索层
│   ├── hierarchical_retriever.py  # 三阶管线: Embedding → Rerank → RL Fusion
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
│   ├── collection_schemas.py      # 集合 Schema (含 RL 字段)
│   └── qdrant/                    # Qdrant 嵌入式后端
│       ├── adapter.py             # QdrantStorageAdapter (标准 + RL)
│       ├── filter_translator.py   # VikingDB DSL → Qdrant Filter
│       └── rl_types.py            # Profile / DecayResult dataclass
│
└── utils/
    ├── uri.py                     # CortexURI 租户隔离 URI 体系
    └── time_utils.py              # 时间工具

plugins/opencortex-memory/         # Claude Code 插件
├── config.json                    # 模式配置 (local/remote)
├── install.sh / uninstall.sh      # 安装/卸载 Hooks
├── hooks/                         # 4 个 Hook 脚本
├── scripts/oc_memory.py           # HTTP client bridge
└── skills/                        # 6 个 Skill 定义

tests/
├── test_e2e_phase1.py             # 24 个 E2E 测试
├── test_mcp_server.py             # 8 个 MCP 测试 (InMemory)
├── test_qdrant_adapter.py         # Qdrant 适配器测试
├── test_rl_integration.py         # 8 个 RL 端到端测试
├── test_mcp_qdrant.py             # 6 个 MCP + Qdrant 测试
├── test_http_server.py            # HTTP Server 测试
├── test_live_servers.py           # 16 个 Live Server 回归测试
├── test_openai_models.py          # OpenAI 嵌入模型测试
└── test_ace_phase{1,2,3}.py       # ACE 自学习引擎测试
```

---

## 运行测试

```bash
# 核心测试 (InMemory, 无外部依赖)
PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server -v

# Qdrant + 真实嵌入 API 测试
PYTHONPATH=src python3 -m unittest tests.test_qdrant_adapter tests.test_rl_integration -v

# MCP + Qdrant 集成测试
PYTHONPATH=src python3 -m unittest tests.test_mcp_qdrant -v

# HTTP Server 测试
PYTHONPATH=src python3 -m unittest tests.test_http_server -v

# Live Server 回归 (需先启动 HTTP + MCP)
PYTHONPATH=src python3 -m unittest tests.test_live_servers -v

# 全量回归
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.10+, async-first |
| 向量存储 | Qdrant (嵌入式本地模式，零外部进程) |
| Embedding | 火山引擎 doubao-embedding-vision (1024 dim) / OpenAI compatible |
| Rerank | Volcengine / Jina / Cohere API 或 LLM fallback |
| LLM | 火山引擎 Ark SDK (doubao-seed) / OpenAI compatible |
| HTTP | FastAPI + uvicorn |
| MCP | PrefectHQ FastMCP v3 (streamable-http / sse / stdio) |
| 包管理 | uv |

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

### [Agentic Context Engine (ACE)](https://github.com/kayba-ai/agentic-context-engine)

Kayba AI 开源的 Agent 自学习上下文引擎。OpenCortex 的 ACE 模块设计受其启发：

- **Skillbook 技能库** — 从对话轨迹中提取可复用技能的核心理念
- **Reflector 反思机制** — LLM 驱动的轨迹分析与技能提炼
- **Trajectory 轨迹管理** — state-action-reward 序列记录与评估

感谢 OpenViking 和 ACE 团队，OpenCortex 站在巨人的肩膀上。
