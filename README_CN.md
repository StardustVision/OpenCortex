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

它不是简单的键值存储，而是一个完整的记忆引擎——三层摘要、语义检索、意图感知路由、强化学习排序。

### 具体能做什么

- **记住**用户偏好、编码规范、历史决策，跨会话保持
- **召回**——每次 Agent 处理新 prompt 时自动搜索并注入相关记忆
- **学习**——有用的记忆通过反馈排名上升，无用的自然衰减
- **提取**——从对话中自动提取可复用技能（零 LLM 开销）
- **隔离**——基于 URI 命名空间实现多租户、多用户数据隔离

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
最终分数 = 语义相似度 + rl_weight * 奖励分数
```

### ACE（Agent 上下文引擎）

自学习子系统。ACE 监控 Agent 存储的内容，自动提取可复用的**技能**——比如「遇到错误 X 时应用修复 Y」或「用户总是要求暗色主题」。这些技能存储在 **Skillbook** 中，搜索时与普通记忆一起返回。

技能通过**置信度评分**系统持续进化：使用反馈调整置信度，**双轨观察**机制让新旧技能变体并行运行、择优留存。

### MCP（模型上下文协议）

一种开放标准，允许 AI Agent 调用外部工具。OpenCortex 通过 Node.js stdio 服务器暴露 13 个 MCP 工具（存储、搜索、反馈等），Claude Code、Cursor 等 MCP 兼容客户端可以直接使用。

### CortexFS

管理三层存储的文件系统抽象。每条记忆变成一个目录，包含 `.abstract.md`（L0）、`.overview.md`（L1）和 `content.md`（L2）文件。CortexFS 处理读写和层级遍历。

### Intent Router（意图路由器）

分析每个搜索查询，确定最优检索策略。简单的是非确认用 3 条 L0 结果回应；深度分析请求用 10 条 L2 结果。先用关键词匹配（零 LLM 开销），复杂查询再可选调用 LLM 分类。

### Qdrant

开源向量数据库。OpenCortex 使用 Qdrant 的**嵌入式模式**——作为进程内库运行，无需独立服务进程。数据自动持久化到本地文件。

### Embedding（嵌入向量）

将文本转换为捕获语义含义的数值向量的过程。OpenCortex 支持火山引擎（doubao-embedding）、OpenAI 等嵌入模型。这些向量驱动语义搜索能力。

### URI 命名空间

每条记忆都有唯一地址：
```
opencortex://{tenant}/user/{user_id}/{type}/{category}/{node_id}
```
确保租户和用户之间的完全数据隔离。

---

## 架构设计

### 系统概览

```
AI Agent (Claude Code / Cursor / Custom)
  |
  |--- MCP 协议 (stdio) ----> Node.js MCP Server ---- HTTP ----> FastAPI HTTP Server (:8921)
  |                            (13 个工具)                              |
  |                                                                     v
  |                                                               MemoryOrchestrator
  |                                                               (统一 API 层)
  |                                                                     |
  |                                                      +--------------+--------------+
  |                                                      |              |              |
  |                                                 IntentRouter   SessionManager   Skillbook
  |                                                      |                            |
  |                                                      v                            v
  |                                               HierarchicalRetriever          Skillbook
  |                                                      |                       (自学习)
  |                                                      v
  |                                               CortexFS + Qdrant Adapter
  |                                               (L0/L1/L2)  (向量 + RL)
  |
  |--- Hooks (生命周期事件) -> Node.js run.mjs
         |-- session-start      -> 启动 HTTP server / 健康检查
         |-- user-prompt-submit -> 主动记忆召回（搜索 API）
         |-- stop               -> 解析 transcript，存储摘要
         |-- session-end        -> 存储会话摘要，停止服务
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
  |-- 异步: RuleExtractor 提取可复用技能 -> Skillbook
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
  |-- RL 融合: final += rl_weight * reward_score
  |-- 可选精排: final = b * rerank + (1-b) * retrieval
  |-- 收敛检测: top-K 稳定 3 轮后停止
  |
  v
返回: { results: [{ uri, abstract, score, overview? }], total }
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
  "tenant_id": "my-team",
  "user_id": "my-name",
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision-250615",
  "embedding_api_key": "YOUR_API_KEY",
  "embedding_api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "http_server_host": "127.0.0.1",
  "http_server_port": 8921
}
```

**无嵌入模型（filter/scroll 降级，RL 排序仍生效）：**

```json
{
  "tenant_id": "my-team",
  "user_id": "my-name",
  "embedding_provider": "none",
  "http_server_port": 8921
}
```

所有字段可通过 `OPENCORTEX_` 前缀的环境变量覆盖：
```bash
export OPENCORTEX_TENANT_ID=my-team
export OPENCORTEX_EMBEDDING_API_KEY=sk-xxx
```

### 3. 启动服务

```bash
uv run opencortex-server --port 8921
```

验证运行状态：
```bash
curl http://localhost:8921/api/v1/memory/health
```

### 4. 安装 Claude Code 插件

在 Claude Code 中：

```
/plugin install
```

选择 `opencortex-memory`。Claude Code 自动注册 Hooks 和 MCP 服务器。Local 模式下，插件在会话开始时自动启动 HTTP 服务器，会话结束时停止。

### 5. Docker 部署

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

### 6. 在其他项目中使用

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
最终分数 = 相似度 + 0.05 * 奖励分数

feedback(uri, reward=+1.0)  ->  未来搜索 +0.05 加成
feedback(uri, reward=-1.0)  ->  -0.05 惩罚
decay()                     ->  reward *= 0.95（受保护: 0.99）
```

### ACE 自学习

RuleExtractor 监控存储的记忆，以零 LLM 开销提取可复用技能：

| 模式 | 检测方式 | 示例 |
|------|---------|------|
| 错误->修复 | 正则: error/traceback + 解决方案 | "遇到 UTF-8 错误时，先用 chardet 检测编码" |
| 用户偏好 | 关键词: always/never/必须 | "必须使用 black 格式化 Python 代码" |
| 工作流 | 3+ 步有序操作 | "lint -> test -> build -> push 部署流程" |

提取的技能存储在 Skillbook 中，搜索时与普通记忆一起返回。

### 技能进化

技能通过反馈驱动的置信度评分和双轨观察机制持续进化：

```
置信度 = 成功率 * log(使用次数 + 1) * 新鲜度衰减

skill_feedback(uri, success=True)  ->  helpful++，重算置信度，版本递增
mine_skills(min_cases=5)           ->  聚类成功案例，LLM 生成技能模板
evolve_skill(uri)                  ->  低置信度技能 → 挖掘替代 → 双轨观察
```

**双轨观察**：替换技能时，旧版本进入"观察"状态，新版本并行运行。累计足够使用后，置信度更高的技能胜出，落败者标记为 deprecated。若替代版本表现不佳，系统自动回滚。

### 上下文自迭代

每次 Agent 响应后，Stop hook 自动：
1. 解析对话 transcript
2. 提取摘要（长对话用 LLM，短对话用本地 fallback）
3. 存储为新记忆

无需手动整理，Agent 的知识库自动增长。

### 多租户隔离

```
opencortex://{tenant}/user/{uid}/{type}/{category}/{node_id}
```

租户和用户之间完全数据隔离。团队级资源可共享，用户级记忆保持私有。通过 `X-Tenant-ID` / `X-User-ID` HTTP 请求头实现按请求身份覆盖。

---

## API 参考

### REST API (HTTP Server)

#### 核心记忆

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/memory/store` | 存储记忆（自动生成 L1、嵌入向量、URI） |
| POST | `/api/v1/memory/search` | 语义搜索（含意图路由和 RL 融合） |
| POST | `/api/v1/memory/feedback` | 提交 RL 反馈（+1 = 有用，-1 = 无用） |
| GET | `/api/v1/memory/stats` | 存储统计和配置信息 |
| POST | `/api/v1/memory/decay` | 触发全局奖励衰减 |
| GET | `/api/v1/memory/health` | 组件健康检查 |

#### 会话

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/session/begin` | 开始新会话 |
| POST | `/api/v1/session/message` | 向会话添加消息 |
| POST | `/api/v1/session/extract_turn` | 仅提取最新轮记忆（会话保持激活） |
| POST | `/api/v1/session/end` | 结束会话，提取并存储记忆 |

#### 技能进化

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/skill/lookup` | 按目标搜索相关技能 |
| POST | `/api/v1/skill/feedback` | 提供使用反馈，更新置信度 |
| POST | `/api/v1/skill/mine` | 从成功案例中挖掘技能 |
| POST | `/api/v1/skill/evolve` | 触发双轨技能进化 |

#### 意图与系统状态

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/intent/should_recall` | 判断查询是否需要检索记忆 |
| GET | `/api/v1/system/status` | 统一 health/stats/doctor 状态 |

### MCP 工具 (13 个)

MCP 服务器暴露与 REST API 相同的能力。主要工具：

- `memory_store` / `memory_batch_store` / `memory_search` / `memory_feedback` / `memory_decay`
- `session_begin` / `session_message` / `session_end`
- `skill_lookup` / `skill_feedback` / `skill_mine` / `skill_evolve`
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
for m in result.memories:
    print(m.uri, m.abstract, m.score)

# 反馈 + 衰减
await orch.feedback(uri=ctx.uri, reward=1.0)
await orch.decay()

# 会话生命周期
await orch.session_begin(session_id="s1")
await orch.session_message("s1", "user", "帮我修复这个 bug")
await orch.session_message("s1", "assistant", "问题是...")
await orch.session_end("s1", quality_score=0.9)  # 自动提取记忆

await orch.close()
```

---

## 插件系统

`plugins/opencortex-memory` 插件结合 Hooks（被动记忆采集）、MCP 服务器（工具代理）和 Skills（主动记忆工具）。全部使用纯 Node.js 实现，零外部依赖。

```
plugins/opencortex-memory/
  hooks/
    handlers/
      session-start.mjs          # 启动 HTTP 服务，初始化状态
      user-prompt-submit.mjs     # 主动记忆召回
      stop.mjs                   # 解析 transcript，存储摘要
      session-end.mjs            # 最终摘要，停止服务
  lib/
    mcp-server.mjs               # MCP stdio 服务器 (13 工具 -> HTTP)
    common.mjs                   # 配置发现、状态管理、uv/python 检测
    http-client.mjs              # native fetch 封装
    transcript.mjs               # JSONL 解析
  skills/                        # 6 个 skill 定义
  bin/oc-cli.mjs                 # CLI: health, status, recall, store
```

### Hook 生命周期

```
SessionStart -----> 启动 HTTP 服务 (local) 或健康检查 (remote)
                    写入 session_state.json
                         |
UserPromptSubmit -> 搜索与 prompt 相关的记忆（3 秒超时）
                    将结果注入 Agent 的系统上下文
                         |
Stop (异步) ------> 解析 transcript，提取轮次摘要
                    POST /api/v1/memory/store（即发即忘）
                         |
SessionEnd -------> 存储会话摘要
                    Kill HTTP 服务 PID (local 模式)
```

---

## 项目结构

```
src/opencortex/
  orchestrator.py                # MemoryOrchestrator（统一 API，~1500 行）
  config.py                      # CortexConfig（dataclass + 环境变量覆盖）
  http/                          # FastAPI 服务器 + 异步客户端
  retrieve/                      # IntentRouter + HierarchicalRetriever + Rerank
  session/                       # SessionManager + MemoryExtractor
  ace/                           # Skillbook + RuleExtractor
  storage/                       # VikingDBInterface + CortexFS + Qdrant adapter
  models/                        # Embedder 抽象 + LLM 工厂

plugins/opencortex-memory/       # Claude Code 插件（纯 Node.js）

tests/                           # 175+ Python 测试 + 8 Node.js 测试
```

---

## 运行测试

```bash
# 核心回归测试 (176 tests, 无外部依赖)
uv run python3 -m unittest tests.test_e2e_phase1 \
  tests.test_ace_phase1 tests.test_ace_phase2 \
  tests.test_rule_extractor tests.test_skill_search_fusion \
  tests.test_case_memory tests.test_skill_evolution -v

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
| Embedding | 火山引擎 doubao-embedding (1024 维) / OpenAI 兼容 |
| HTTP | FastAPI + uvicorn |
| 包管理 | uv |

## License

[Apache-2.0](LICENSE)

## 致谢

OpenCortex 从以下开源项目移植并重构：

- [OpenViking](https://github.com/volcengine/openviking) — CortexFS 三层存储、层级检索算法、VikingDBInterface 存储抽象
- [Agentic Context Engine (ACE)](https://github.com/kayba-ai/agentic-context-engine) — Skillbook 概念、Reflector 反思机制、轨迹管理
