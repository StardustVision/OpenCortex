<h1 align="center">OpenCortex</h1>
<p align="center"><strong>面向 AI Agent 的持久记忆与上下文基础设施</strong></p>
<p align="center">
  <a href="#什么是-opencortex">简介</a> &middot;
  <a href="#核心概念">核心概念</a> &middot;
  <a href="#架构概览">架构</a> &middot;
  <a href="#快速开始">快速开始</a> &middot;
  <a href="#核心能力">特性</a> &middot;
  <a href="#api-概览">API</a> &middot;
  <a href="#仓库结构">仓库</a> &middot;
  <a href="README.md">English</a>
</p>

---

## 什么是 OpenCortex

LLM Agent 会遗忘。会话上下文、用户偏好、设计结论、排障过程和可复用工作流，如果不落到模型上下文窗口之外，就会在会话结束后消失。

OpenCortex 就是解决这个问题的持久化层。它把分层记忆存储、面向意图的召回规划和适合 Agent 工作流的检索能力组合起来，并通过统一的 HTTP 后端和 MCP 接入包对外提供服务。

它主要面向这些场景：

- 跨会话记忆和项目上下文
- 文档与对话写入
- 兼顾相关性、时间性、反馈信号和结构信息的检索
- 建立在同一底座上的可选知识、洞察和技能服务
- 基于 JWT 身份的多租户与项目级隔离

## 核心概念

### 三层记忆

每条记录会保存为多个细节层级：

| 层级 | 作用 |
|---|---|
| `L0` | 轻量摘要，用于低成本索引和快速确认 |
| `L1` | 结构化概览，适合作为默认召回结果 |
| `L2` | 完整内容，用于深入分析和审计 |

### 显式召回规划

OpenCortex 不把所有查询都当成普通向量检索。查询会先分类、路由，再生成召回计划，决定是否召回、搜索哪些上下文，以及返回多少细节。

### 不止向量相似度的检索

搜索不依赖单一向量分数。根据配置和查询类型，排序可以融合语义检索、词法权重、精排门控、显式反馈、热度，以及围绕共享实体扩展的锥形检索信号。

### 上下文生命周期

中心生命周期端点是 `/api/v1/context`，负责三个阶段：

- `prepare`：规划召回并返回记忆或知识上下文
- `commit`：记录当前轮次和反馈信号
- `end`：收尾会话状态并触发可选后处理

### 共享记忆底座

核心记忆、可选知识提取、洞察报告和技能引擎共用同一套存储、身份和检索基础，而不是分别搭建独立系统。

## 架构概览

```text
AI 客户端
  -> MCP 包（plugins/opencortex-memory/）
  -> FastAPI 服务
  -> MemoryOrchestrator
     -> 记忆 / 文档 / 对话写入管线
     -> 召回规划与检索
     -> CortexFS + 嵌入式 Qdrant 存储
     -> 可选知识 / 洞察 / 技能服务
  -> 可选 Web 控制台 /console
```

整体上，Agent 先接入 MCP 包，MCP 包再访问 FastAPI 后端；后端统一协调存储、召回、上下文生命周期和可选分析服务。

## 快速开始

### 环境要求

- Python `>=3.10`
- Node.js `>=18`
- `uv`

### 1. 安装

```bash
git clone --recurse-submodules https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
uv sync
```

### 2. 启动后端

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921
```

需要时生成或查看 token：

```bash
uv run opencortex-token generate
uv run opencortex-token list
```

### 3. 连接 MCP 客户端

Claude Code：

```bash
claude mcp add opencortex -- npx -y opencortex-memory
```

Codex CLI：

```bash
codex mcp add opencortex -- npx -y opencortex-memory
```

Gemini CLI：

```bash
gemini mcp add opencortex -- npx -y opencortex-memory
```

然后运行初始化向导：

```bash
npx opencortex-cli setup
```

MCP 配置会写入 `./mcp.json` 或 `~/.opencortex/mcp.json`，取决于使用模式和作用范围。

### 4. Docker 方式

```bash
docker compose up -d
docker compose logs -f
```

如果前端构建产物已经存在，控制台可通过 `http://127.0.0.1:8921/console` 访问。

## 核心能力

OpenCortex 的核心能力集中在同一套记忆底座之上：它既能处理短记忆、文档和对话，又围绕 `/api/v1/context` 提供显式召回规划和生命周期处理；检索可融合语义、词法、反馈、热度与结构化信号，同时还能在同一后端上扩展知识、洞察和技能工作流，并通过请求身份强制执行租户、用户和项目范围隔离。

## API 概览

OpenCortex 的 API 范围比这份 README 更广。最重要的几个分组是：

- 记忆：记忆、文档与对话的持久化和检索
- 上下文 / 会话：围绕 `/api/v1/context` 的 Agent 生命周期
- 内容 / 可观测性：分层内容读取，以及健康和诊断相关能力
- 知识 / 洞察 / 技能：建立在同一后端上的可选高阶工作流
- 鉴权 / 管理：身份、令牌、诊断与管理员维护能力

如果你要继续查看具体路由，建议直接从这些目录进入：

- `src/opencortex/http/`
- `src/opencortex/skill_engine/`
- `src/opencortex/insights/`

## 仓库结构

仓库顶层主要分为几块：`src/opencortex/` 承载核心后端，`web/` 提供可选控制台，`plugins/opencortex-memory/` 是 MCP 集成层子模块，`tests/` 负责自动化验证，`docs/`、`scripts/`、`examples/` 则提供文档、运维辅助脚本和示例集成。

## 深入阅读

- [三层存储](docs/architecture/three-layer-storage-cn.md)
- [锥形检索](docs/architecture/cone-retrieval-cn.md)
- [自噬式记忆流程与上下文生命周期](docs/architecture/autophagy-cn.md)
- [技能引擎](docs/architecture/skill-engine-cn.md)

## 测试

```bash
uv run --group dev pytest
```

`plugins/opencortex-memory/` 下的 MCP 包也有独立的 Node.js 测试集。

## 技术栈

OpenCortex 主要由 Python/FastAPI 后端、CortexFS + 嵌入式 Qdrant 存储、Node.js MCP 接入包，以及可选的 React/Vite 控制台组成。

## License

Apache-2.0
