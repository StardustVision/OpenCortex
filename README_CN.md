<h1 align="center">OpenCortex</h1>
<p align="center"><strong>面向 AI Agent 的持久记忆与上下文管理系统</strong></p>
<p align="center">
  <a href="#什么是-opencortex">简介</a> &middot;
  <a href="#核心概念">核心概念</a> &middot;
  <a href="#架构设计">架构</a> &middot;
  <a href="#快速开始">快速开始</a> &middot;
  <a href="#核心能力">特性</a> &middot;
  <a href="#api-参考">API</a> &middot;
  <a href="../README.md">English</a>
</p>

---

## 什么是 OpenCortex

大模型 Agent 的上下文窗口是有限的。每次对话结束，Agent 学到的一切——用户偏好、调试方案、架构决策——全部丢失，下次对话从零开始。

OpenCortex 赋予 Agent **持久的、可检索的、会自我进化的记忆**。可以把它理解为 AI 的长期记忆：Agent 存储学到的知识，在需要时自动召回相关上下文，并通过强化学习让最有价值的记忆优先浮现。

它不是简单的键值存储，而是一个完整的记忆引擎——三层摘要、语义检索、意图感知路由、强化学习排序、对话知识自动提取。

### 具体能做什么

- **记住**用户偏好、编码规范、历史决策，跨会话保持
- **召回**——每次 Agent 处理新 prompt 时自动搜索并注入相关记忆
- **学习**——有用的记忆通过反馈排名上升，无用的自然衰减
- **提取**——通过 Cortex Alpha 管线从对话轨迹自动提取可复用知识
- **隔离**——基于 URI 命名空间实现多租户、多用户数据隔离
- **可移植**——单个 `memory_context` MCP 工具替代平台特定的 hooks

---

## 核心概念

### 记忆层级：L0 / L1 / L2

OpenCortex 将每条记忆存储为三个精度层级，最大程度减少 Token 消耗：

| 层级 | 内容 | Token 开销 | 使用场景 |
|------|------|-----------|---------|
| **L0**（摘要） | 一句话概括 | ~20-50 | 向量检索索引，快速确认 |
| **L1**（概要） | 一段话，含推理和上下文 | ~100-200 | 大多数检索场景（默认） |
| **L2**（全文） | 完整原始内容 | 不限 | 深度分析、审计 |

存储时自动生成 L1。检索时系统只返回满足查询需要的最低层级——90% 的查询用 L0 或 L1 即可。

### SONA（自组织神经注意力）

基于强化学习的记忆排序系统。Agent 给记忆正反馈时，其奖励分数增加，在未来的搜索中排名更高。长期不用的记忆自然衰减。公式：

```
最终分数 = beta * 精排分数 + (1 - beta) * 检索分数 + reward_weight * 奖励分数
```

### Cortex Alpha

知识提取管线。会话结束时，Cortex Alpha 自动执行：

1. **Observer** 实时记录对话 transcript
2. **TraceSplitter** 通过 LLM 将 transcript 拆解为离散任务轨迹
3. **TraceStore** 将轨迹持久化到 Qdrant
4. **Archivist** 从轨迹中提取可复用知识候选
5. **Sandbox** 质量门控（统计 + LLM 验证）
6. **KnowledgeStore** 持久化已批准的知识，支持搜索

无需手动整理，Agent 的知识库自动增长。

### Memory Context Protocol（记忆上下文协议）

平台无关的三阶段生命周期，替代 Claude Code hooks：

| 阶段 | 时机 | 作用 |
|------|------|------|
| **prepare** | 生成响应前 | 召回相关记忆和知识，返回上下文给 Agent |
| **commit** | 生成响应后 | 记录对话轮次，对引用的记忆施加 RL 奖励 |
| **end** | 会话结束 | 刷新 transcript，触发轨迹拆分和知识提取 |

任何 MCP 兼容客户端都可以使用单个 `memory_context` 工具——无需 hooks。

### MCP（模型上下文协议）

一种开放标准，允许 AI Agent 调用外部工具。OpenCortex 通过 Node.js stdio 服务器暴露 9 个 MCP 工具，Claude Code、Cursor 等 MCP 兼容客户端可以直接使用。

### CortexFS

管理三层存储的文件系统抽象。每条记忆变成一个目录，包含 `.abstract.md`（L0）、`.overview.md`（L1）和 `content.md`（L2）文件。CortexFS 处理读写和层级遍历。

### Intent Router（意图路由器）

分析每个搜索查询，确定最优检索策略。简单的是非确认用 3 条 L0 结果回应；深度分析请求用 10 条 L2 结果。先用关键词匹配（零 LLM 开销），复杂查询再可选调用 LLM 分类。

### Qdrant

开源向量数据库。OpenCortex 使用 Qdrant 的**嵌入式模式**——作为进程内库运行，无需独立服务进程。数据自动持久化到本地文件。

### Embedding（嵌入向量）

将文本转换为捕获语义含义的数值向量的过程。OpenCortex 支持本地嵌入（multilingual-e5-large via FastEmbed）、火山引擎（doubao-embedding）、OpenAI 等。本地精排也已支持（jina-reranker-v2-base-multilingual）。

### URI 命名空间

每条记忆都有唯一地址：
```
opencortex://{tenant}/{user_id}/{type}/{category}/{node_id}
```
确保租户和用户之间的完全数据隔离。

---

## 架构设计

### 系统概览

```
AI Agent (Claude Code / Cursor / Custom)
  |
  |--- MCP 协议 (stdio) ----> Node.js MCP Server ---- HTTP ----> FastAPI HTTP Server (:8921)
  |                            (9 个工具)                               |
  |                                                                     v
  |                                                               MemoryOrchestrator
  |                                                               (统一 API 层)
  |                                                                     |
  |                                                      +--------------+--------------+
  |                                                      |              |              |
  |                                                 IntentRouter   ContextManager   Observer
  |                                                      |         (prepare/       (transcript
  |                                                      v          commit/end)     记录)
  |                                               HierarchicalRetriever     |
  |                                                      |                  v
  |                                                      v            TraceSplitter → Archivist
  |                                               CortexFS + Qdrant       → KnowledgeStore
  |                                               (L0/L1/L2)  (向量 + RL)
  |
  |    (身份信息来自 JWT Bearer token → RequestContextMiddleware → contextvars)
```

### 数据流：存储

```
Agent 调用 memory_store(abstract="用户偏好暗色主题", content="...")
  |
  v
MemoryOrchestrator.add()
  |-- 生成嵌入向量 (1024 维)
  |-- 自动生成 L1 概要（短内容直接复用；长内容 LLM 摘要）
  |-- 写入 CortexFS:  .abstract.md / .overview.md / content.md
  |-- 写入 Qdrant:    向量 + 元数据 + RL 字段 (reward_score=0)
  |
  v
返回: { uri, context_type, category, abstract }
```

### 数据流：搜索

```
Agent 调用 memory_search(query="用户喜欢什么主题?")
  |
  v
IntentRouter (三层分析)
  |-- 第一层: 关键词提取（零 LLM 开销）
  |-- 第二层: LLM 分类（可选，复杂查询）
  |-- 第三层: 记忆触发器（自动追加类别查询）
  |-- 输出: intent_type=quick_lookup, top_k=3, detail_level=L0
  |
  v
HierarchicalRetriever
  |-- 查询嵌入 -> Qdrant 向量搜索
  |-- 前沿批处理: 基于波次的并行目录遍历
  |-- 分数传播: child_score = a * child + (1-a) * parent
  |-- RL 融合: final += reward_weight * reward_score
  |-- 可选精排: final = b * rerank + (1-b) * retrieval
  |-- 收敛检测: top-K 稳定 3 轮后停止
  |
  v
返回: { results: [{ uri, abstract, score, overview? }], total }
```

### 数据流：Memory Context Protocol

```
Agent 调用 memory_context(phase="prepare", session_id="s1", turn_id="t1",
                          messages=[{role: "user", content: "..."}])
  |
  v
ContextManager._prepare()
  |-- 自动创建会话（如未激活则 Observer.begin_session）
  |-- IntentRouter.route(query) → SearchIntent
  |-- orchestrator.search() → 记忆条目
  |-- orchestrator.knowledge_search() → 知识条目
  |-- 返回 { memory, knowledge, instructions, intent }
  |
  v  (Agent 生成响应)
  v
Agent 调用 memory_context(phase="commit", session_id="s1", turn_id="t1",
                          messages=[...], cited_uris=[...])
  |
  v
ContextManager._commit()
  |-- Observer.record_batch() → 内存 transcript 缓冲
  |-- 异步: 对 cited_uris 施加 RL 奖励
  |-- 返回 { accepted: true }
  |
  v  (会话结束)
  v
Agent 调用 memory_context(phase="end", session_id="s1")
  |
  v
ContextManager._end()
  |-- Observer.flush() → TraceSplitter → TraceStore
  |-- Archivist → Sandbox → KnowledgeStore
  |-- 清理所有会话状态
  |-- 返回 { status: "closed", traces, knowledge_candidates }
```

### 数据流：反馈闭环

```
Agent 调用 memory_feedback(uri="opencortex://...", reward=1.0)
  |
  v
更新 Qdrant RL 字段
  |-- reward_score += 1.0
  |-- positive_feedback_count += 1
  |
  v
下次搜索: 该记忆排名更高 (score + 0.05 * reward)
随时间: apply_decay() 降低不活跃记忆的分数 (0.95x / 周期)
```

### 部署模式

| 模式 | 工作方式 | 适用场景 |
|------|---------|---------|
| **Local**（默认） | SessionStart hook 自动启动 HTTP 服务；MCP 由 Claude Code 管理 | 个人开发 |
| **Remote** | 连接预部署的 HTTP 服务器；客户端无需 Python | 团队共享、服务器部署 |
| **Docker** | `docker compose up`，配置文件通过 volume 挂载 | 生产部署 |

---

## 快速开始

### 前置条件

| 工具 | 版本 | 用途 |
|------|------|------|
| **Python** | >= 3.10 | HTTP 服务器后端 |
| **Node.js** | >= 18 | MCP 服务器和插件 Hooks |
| **uv** | 最新 | Python 包管理器（[安装指南](https://docs.astral.sh/uv/getting-started/installation/)） |

### 1. 克隆并安装

```bash
git clone https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
uv sync
```

`uv sync` 会自动创建虚拟环境、安装所有依赖、注册 `opencortex-server` 命令。

### 2. 配置

创建配置文件。系统按以下顺序搜索：

1. `./server.json`（项目目录）
2. `~/.opencortex/server.json`（全局，缺失时自动创建）

**有嵌入模型（完整语义搜索）：**

```json
{
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision-250615",
  "embedding_api_key": "YOUR_API_KEY",
  "embedding_api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "http_server_host": "127.0.0.1",
  "http_server_port": 8921
}
```

**本地嵌入（无需 API Key）：**

```json
{
  "embedding_provider": "local",
  "http_server_port": 8921
}
```

所有服务端字段可通过 `OPENCORTEX_` 前缀的环境变量覆盖：
```bash
export OPENCORTEX_EMBEDDING_API_KEY=sk-xxx
```

### 3. 生成 Token

身份信息（租户 + 用户）嵌入 JWT Token：

```bash
uv run opencortex-token generate
# 按提示输入 tenant_id 和 user_id
# Token 保存到 {data_root}/tokens.json

# Docker 环境：
docker exec -it opencortex-server uv run opencortex-token generate
```

管理 Token：
```bash
uv run opencortex-token list       # 查看已发行的 Token
uv run opencortex-token revoke <prefix>  # 按前缀撤销
```

### 4. 启动服务

```bash
uv run opencortex-server --port 8921
```

验证运行状态：
```bash
curl http://localhost:8921/api/v1/memory/health
```

### 5. 安装 Claude Code 插件

在 Claude Code 中：

```
/plugin install
```

选择 `opencortex-memory`。Claude Code 自动注册 Hooks 和 MCP 服务器。Local 模式下，插件在会话开始时自动启动 HTTP 服务器，会话结束时停止。

### 6. Docker 部署

```bash
# 构建并启动
docker compose up -d

# 查看日志
docker compose logs -f opencortex

# 验证
curl http://localhost:8921/api/v1/memory/health
```

使用配置文件时，取消 `docker-compose.yml` 中的 volume 注释：
```yaml
volumes:
  - ./server.json:/app/server.json:ro
```

### 7. 配置 MCP 客户端

创建 `mcp.json`（项目目录或 `~/.opencortex/mcp.json`）：

```json
{
  "token": "<第3步生成的jwt-token>",
  "mode": "local",
  "local": { "http_port": 8921 }
}
```

远程服务器：
```json
{
  "token": "<jwt-token>",
  "mode": "remote",
  "remote": { "http_url": "http://your-server:8921" }
}
```

### 8. 在其他项目中使用

在任意项目根目录添加 `.mcp.json`，连接 OpenCortex 实例：

```json
{
  "mcpServers": {
    "opencortex": {
      "command": "node",
      "args": ["/path/to/plugins/opencortex-memory/lib/mcp-server.mjs"]
    }
  }
}
```

---

## 核心能力

### 三层摘要 (L0 / L1 / L2)

每条记忆存储为三个精度层级，系统自动选择满足查询的最低开销层级：

```
L0 摘要  ->  "用户偏好暗色主题"                              ~30 tokens
L1 概要  ->  "在 10+ 次会话中一致表达。适用于 VS Code、        ~150 tokens
              终端和浏览器工具。属于强偏好。"
L2 全文  ->  [讨论该偏好的完整对话摘录]                       ~500+ tokens
```

`add()` 自动生成 L1：短内容直接复用，长内容由 LLM 摘要（无 LLM 时截断）。

### 意图感知检索

Intent Router 分析每个查询，自动选择检索策略：

| 意图类型 | 触发条件 | Top-K | 详情层级 | 示例 |
|---------|---------|-------|---------|------|
| `quick_lookup` | 简短确认性查询 | 3 | L0 | "用户喜欢暗色主题吗？" |
| `recent_recall` | 时间指示词 | 5 | L1 | "上次讨论了什么？" |
| `deep_analysis` | 需要完整上下文 | 10 | L2 | "详细回顾认证系统设计" |
| `summarize` | 聚合类关键词 | 30 | L1 | "总结最近的架构变更" |

### SONA 强化学习排序

正反馈提升记忆分数，负反馈抑制。时间衰减确保陈旧记忆淡出：

```
最终分数 = beta * 精排分数 + (1-beta) * 检索分数 + reward_weight * 奖励分数

feedback(uri, reward=+1.0)  ->  未来搜索 +0.05 加成
feedback(uri, reward=-1.0)  ->  -0.05 惩罚
decay()                     ->  reward *= 0.95（受保护: 0.99）
```

### 知识提取（Cortex Alpha）

会话结束时，Alpha 管线自动提取可复用知识：

```
Observer transcript → TraceSplitter (LLM) → 任务轨迹
                                              |
                                     Archivist (LLM) → 知识候选
                                              |
                                     Sandbox（质量门控）→ 已批准知识
                                              |
                                     KnowledgeStore（向量搜索）
```

知识可通过 `knowledge_search` 搜索，在 `memory_context` prepare 阶段与记忆一起返回。

### Memory Context Protocol（记忆上下文协议）

平台无关的生命周期，适用于任何 MCP 客户端：

```python
# 1. 生成响应前——获取相关上下文
prepare = memory_context(phase="prepare", session_id="s1", turn_id="t1",
                         messages=[{"role": "user", "content": "..."}])
# 返回: { memory: [...], knowledge: [...], instructions: {...} }

# 2. 生成响应后——记录对话轮次
memory_context(phase="commit", session_id="s1", turn_id="t1",
               messages=[...user + assistant...], cited_uris=["opencortex://..."])
# 返回: { accepted: true }

# 3. 完成后——关闭会话
memory_context(phase="end", session_id="s1")
# 返回: { status: "closed", traces: 3, knowledge_candidates: 1 }
```

特性：按 `(session_id, turn_id)` 幂等、空闲会话自动关闭、失败时写 fallback JSONL、异步 RL 奖励引用的 URI。

### 多租户隔离

```
opencortex://{tenant}/{uid}/{type}/{category}/{node_id}
```

租户和用户之间完全数据隔离。团队级资源可共享，用户级记忆保持私有。身份信息从 JWT Bearer Token 的 claims（`tid`/`uid`）中提取。

---

## API 参考

### REST API (HTTP Server)

#### 核心记忆

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/memory/store` | 存储记忆（自动生成 L1、嵌入向量、URI） |
| POST | `/api/v1/memory/batch_store` | 批量存储文档 |
| POST | `/api/v1/memory/search` | 语义搜索（含意图路由和 RL 融合） |
| POST | `/api/v1/memory/feedback` | 提交 RL 反馈（+1 = 有用，-1 = 无用） |
| GET | `/api/v1/memory/stats` | 存储统计和配置信息 |
| POST | `/api/v1/memory/decay` | 触发全局奖励衰减 |
| GET | `/api/v1/memory/health` | 组件健康检查 |

#### 上下文协议

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/context` | 统一生命周期：prepare / commit / end |

#### 会话

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/session/begin` | 开始新会话（Observer 记录） |
| POST | `/api/v1/session/message` | 向会话添加消息 |
| POST | `/api/v1/session/end` | 结束会话，触发轨迹拆分和知识提取 |

#### 知识（Cortex Alpha）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/knowledge/search` | 搜索已批准知识 |
| POST | `/api/v1/knowledge/approve` | 批准知识候选 |
| POST | `/api/v1/knowledge/reject` | 拒绝知识候选 |
| GET | `/api/v1/knowledge/candidates` | 列出待审知识候选 |
| POST | `/api/v1/archivist/trigger` | 手动触发知识提取 |

#### 系统

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/intent/should_recall` | 判断查询是否需要检索记忆 |
| GET | `/api/v1/system/status` | 统一 health/stats/doctor 状态 |

### MCP 工具 (9 个)

MCP 服务器暴露与 REST API 相同的能力：

- `store` / `batch_store` / `search` / `feedback` / `decay`
- `recall`（prepare → 搜索 → 返回上下文）
- `add_message`（提交对话轮次到 Observer）
- `end`（关闭会话 → 轨迹拆分 → 知识提取）
- `system_status`

### Python API

```python
from opencortex import MemoryOrchestrator, CortexConfig, init_config

init_config(CortexConfig())
orch = MemoryOrchestrator(embedder=my_embedder)
await orch.init()

# 存储（自动生成 L1 概要）
ctx = await orch.add(
    abstract="用户偏好暗色主题",
    content="所有编辑器和终端使用暗色主题，包括 VS Code、Vim、iTerm2。",
    category="preferences",
)

# 搜索（Intent Router 自动选择策略）
result = await orch.search("用户喜欢什么主题？")
for m in result:
    print(m.uri, m.abstract, m.score)

# 反馈 + 衰减
await orch.feedback(uri=ctx.uri, reward=1.0)
await orch.decay()

# 会话生命周期
await orch.session_begin(session_id="s1")
await orch.session_message("s1", "user", "帮我修复这个 bug")
await orch.session_message("s1", "assistant", "问题是...")
await orch.session_end("s1", quality_score=0.9)

# 知识搜索
results = await orch.knowledge_search("部署工作流")

await orch.close()
```

---

## 插件系统

`plugins/opencortex-memory` 插件提供 MCP 服务器（工具代理，适用于任何 MCP 兼容客户端）。全部使用纯 Node.js 实现，零外部依赖。

```
plugins/opencortex-memory/
  lib/
    mcp-server.mjs               # MCP stdio 服务器 (9 工具 -> HTTP)
    common.mjs                   # 配置发现、状态管理、uv/python 检测
    http-client.mjs              # native fetch 封装 + Bearer Token 认证
    transcript.mjs               # JSONL 解析
  bin/oc-cli.mjs                 # CLI: health, status, recall, store
```

### 会话生命周期

MCP 服务器通过 `recall` / `add_message` / `end` 工具内部管理会话生命周期：

```
recall（响应前）→ add_message（响应后）→ end（会话结束）
```

无需 hooks，任何 MCP 兼容客户端均可使用。

---

## 项目结构

```
src/opencortex/
  orchestrator.py                # MemoryOrchestrator（统一 API）
  config.py                      # CortexConfig（dataclass + 环境变量覆盖）
  http/                          # FastAPI 服务器 + 异步客户端 + 请求上下文
  retrieve/                      # IntentRouter + HierarchicalRetriever + Rerank
  context/                       # ContextManager（Memory Context Protocol）
  alpha/                         # Cortex Alpha: Observer, TraceSplitter, Archivist, KnowledgeStore
  storage/                       # VikingDBInterface + CortexFS + Qdrant adapter
  models/                        # Embedder 抽象（本地/API）+ LLM 工厂

plugins/opencortex-memory/       # Claude Code 插件（纯 Node.js）

tests/                           # 140+ Python 测试 + 8 Node.js 测试
```

---

## 运行测试

```bash
# 核心测试（无外部依赖）
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_write_dedup tests.test_context_manager -v

# MCP 服务器测试（需要运行中的 HTTP 服务器）
node --test tests/test_mcp_server.mjs

# 全量回归
uv run python3 -m unittest discover -s tests -v
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python 3.10+，async-first |
| 插件和 MCP | Node.js >= 18，纯 ESM，零外部依赖 |
| 向量存储 | Qdrant（嵌入式本地模式，无独立进程） |
| Embedding | 本地 (multilingual-e5-large) / 火山引擎 / OpenAI 兼容 |
| 精排 | 本地 (jina-reranker-v2-base-multilingual) / API |
| HTTP | FastAPI + uvicorn |
| 包管理 | uv |

## License

[Apache-2.0](LICENSE)

## 致谢

OpenCortex 从以下开源项目移植并重构：

- [OpenViking](https://github.com/volcengine/openviking) — CortexFS 三层存储、层级检索算法、VikingDBInterface 存储抽象
