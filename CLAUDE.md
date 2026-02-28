# OpenCortex - Project Guide

## 项目概述

OpenCortex 是面向 AI Agent 的**记忆与上下文管理系统**，从 [OpenViking](https://github.com/volcengine/openviking) 移植并重构。

核心能力：
- **三层摘要** (L0/L1/L2) — 按需返回精度层级，节省 Token
- **强化学习排序** — RL 驱动，高价值记忆上浮，低价值自然衰减
- **租户级隔离** — 多团队多用户，URI 命名空间隔离
- **MCP Server** — 通过 FastMCP v3 暴露工具给外部 Agent
- **HTTP Server** — FastAPI 独立部署，承载 Orchestrator 业务逻辑

## 技术栈

- Python 3.10+, async-first (HTTP Server 后端)
- Node.js >= 18 (MCP Server + Plugin Hooks, 零外部依赖)
- 向量存储: Qdrant (嵌入式本地模式，零外部进程)
- Embedding: 火山引擎 doubao-embedding-vision (1024 dim)
- HTTP: FastAPI + uvicorn + httpx
- MCP: Node.js stdio proxy (25 tools → HTTP API)
- 测试: unittest (103 Python) + node:test (8 Node.js MCP)

## 关键目录

```
src/opencortex/           # 核心框架 (Python)
  config.py               # CortexConfig (tenant/user)
  orchestrator.py          # MemoryOrchestrator 顶层 API
  http/
    server.py              # FastAPI HTTP Server
    client.py              # OpenCortexClient (异步 HTTP 客户端)
    models.py              # Pydantic 请求模型
  storage/
    vikingdb_interface.py  # 抽象接口 (25 async 方法)
    cortex_fs.py           # CortexFS 文件系统抽象 (原 VikingFS)
    qdrant/adapter.py      # QdrantStorageAdapter (Qdrant 嵌入式)
    qdrant/filter_translator.py  # VikingDB DSL → Qdrant Filter
  retrieve/
    hierarchical_retriever.py  # 层级递归检索
  ace/
    engine.py              # ACEngine 自学习引擎
    skillbook.py           # Skillbook CRUD + 向量搜索
    rule_extractor.py      # RuleExtractor 零 LLM 规则提取
    reflector.py           # LLM 反思 (可选)
    skill_manager.py       # LLM 策略管理 (可选)
  models/embedder/         # 嵌入模型抽象

plugins/opencortex-memory/ # Claude Code 插件 (纯 Node.js)
  hooks/run.mjs            # Hook 统一入口
  hooks/handlers/*.mjs     # 4 个 Hook 处理器
  lib/common.mjs           # 配置/状态/路径
  lib/http-client.mjs      # fetch 封装
  lib/transcript.mjs       # JSONL 解析
  lib/mcp-server.mjs       # MCP stdio server (25 tools)
  bin/oc-cli.mjs            # CLI 工具

tests/
  test_e2e_phase1.py       # 24 个 E2E 测试
  test_mcp_server.mjs      # 8 个 MCP 测试 (Node.js)
  test_ace_phase1.py       # 21 个 ACE 测试
  test_ace_phase2.py       # 17 个 ACE Phase 2 测试
  test_rule_extractor.py   # 20 个规则提取测试
  test_skill_search_fusion.py   # 11 个 Skill 融合搜索测试
  test_integration_skill_pipeline.py  # 10 个 Qdrant 集成测试

docs/architecture.md       # 架构设计文档
```

## 开发约定

- 所有存储操作通过 `VikingDBInterface` 抽象，方法均为 `async`
- URI 格式: `opencortex://{team}/user/{uid}/{type}/{category}/{node_id}`
- 配置优先从 `opencortex.json` 加载
- 强化学习方法 (update_reward/get_profile/apply_decay/set_protected) 不在接口中，通过 `hasattr` 检测
- 包管理使用 `uv` (不用 pip)
- 运行 Python 测试: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion tests.test_integration_skill_pipeline -v`
- 运行 MCP 测试: `node --test tests/test_mcp_server.mjs`
- VikingFS 已重命名为 CortexFS，旧名保留向后兼容

## 架构

调用链:
- MCP: Agent → node mcp-server.mjs (stdio) → fetch → HTTP Server (FastAPI) → Orchestrator → Qdrant
- Hooks: Agent → node run.mjs <hook> → fetch → HTTP Server

自学习闭环:
- `memory_store (add)` → RuleExtractor 异步提取 skill → Skillbook 持久化
- `memory_search (search)` → 并行搜索 contexts + skillbooks → 混合排序返回
- `memory_feedback (feedback)` → 更新 RL reward / Skillbook tag

MCP Server 为纯 Node.js stdio 代理，由 Claude Code 通过 .mcp.json 自动管理生命周期。

## HTTP Server

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921
```

## 当前状态

已完成: 核心框架 + HTTP Server + Node.js MCP Server + Node.js Hooks + ACE 自学习闭环 + 103 Python 测试 + 8 Node.js MCP 测试
待实现: 真实 Embedding 接入, 远程同步, Session End LLM 反思 (config 控制)

## 记忆召回策略

当 OpenCortex 记忆系统可用时（由 hook systemMessage 提示），遵循以下策略：

**何时调用 `memory_search`**：
- 用户提到过去的决定、偏好、约定
- 任务需要项目上下文或历史信息
- 遇到之前解决过的类似问题
- 用户显式要求回忆/查找之前的内容

**何时不调用**：
- 简单问候、闲聊、确认
- 纯粹的代码生成（无需历史上下文）
- 用户已提供完整上下文

**使用方式**：
- 工具: `memory_search(query="...", limit=5)`
- 可选过滤: `context_type` ("memory"/"resource"/"skill"), `category`
- 结果中 score > 0.7 的记忆优先参考
- 有用的记忆可用 `memory_feedback(uri="...", reward=1.0)` 正向反馈
