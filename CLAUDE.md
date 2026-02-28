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

- Python 3.10+, async-first
- 向量存储: Qdrant (嵌入式本地模式，零外部进程)
- Embedding: 火山引擎 doubao-embedding-vision (1024 dim)
- MCP: PrefectHQ FastMCP v3 (`fastmcp>=3.0`)
- HTTP: FastAPI + uvicorn + httpx
- 测试: unittest (111 个用例全过)

## 关键目录

```
src/opencortex/           # 核心框架
  config.py               # CortexConfig (tenant/user/mcp)
  orchestrator.py          # MemoryOrchestrator 顶层 API
  mcp_server.py            # FastMCP Server (双模式: remote/local)
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

tests/
  test_e2e_phase1.py       # 24 个 E2E 测试
  test_mcp_server.py       # 8 个 MCP 测试
  test_ace_phase1.py       # 21 个 ACE 测试
  test_ace_phase2.py       # 17 个 ACE Phase 2 测试
  test_rule_extractor.py   # 20 个规则提取测试
  test_skill_search_fusion.py   # 11 个 Skill 融合搜索测试
  test_integration_skill_pipeline.py  # 10 个 Qdrant 集成测试

docs/architecture.md       # 架构设计文档
```

## 开发约定

- 所有存储操作通过 `VikingDBInterface` 抽象，方法均为 `async`
- URI 格式: `opencortex://tenant/{team}/user/{uid}/{type}/{category}/{node_id}`
- 配置优先从 `opencortex.json` 加载
- 强化学习方法 (update_reward/get_profile/apply_decay/set_protected) 不在接口中，通过 `hasattr` 检测
- 包管理使用 `uv` (不用 pip)
- 运行测试: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion tests.test_integration_skill_pipeline -v`
- VikingFS 已重命名为 CortexFS，旧名保留向后兼容

## 架构

调用链: Agent → MCP Server → HTTP Server (FastAPI) → Orchestrator → CortexFS → Qdrant

自学习闭环:
- `memory_store (add)` → RuleExtractor 异步提取 skill → Skillbook 持久化
- `memory_search (search)` → 并行搜索 contexts + skillbooks → 混合排序返回
- `memory_feedback (feedback)` → 更新 RL reward / Skillbook tag

MCP 支持双模式:
- `remote` (默认): MCP 作为薄客户端，转发到 HTTP Server
- `local`: MCP 内嵌 Orchestrator，适合开发调试

## MCP Server

```bash
# stdio (本地)
python -m opencortex.mcp_server --config opencortex.json

# SSE (远程)
python -m opencortex.mcp_server --transport sse --port 8920
```

## HTTP Server

```bash
python -m opencortex.http --host 127.0.0.1 --port 8921
```

## 当前状态

已完成: 核心框架 + MCP Server + HTTP Server + ACE 自学习闭环 + 111 测试
待实现: 真实 Embedding 接入, 远程同步, Session End LLM 反思 (config 控制)
