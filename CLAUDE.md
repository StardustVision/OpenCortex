# OpenCortex - Project Guide

## 项目概述

OpenCortex 是面向 AI Agent 的**记忆与上下文管理系统**，从 [OpenViking](https://github.com/volcengine/openviking) 移植并重构。

核心能力：
- **三层摘要** (L0/L1/L2) — 按需返回精度层级，节省 Token
- **SONA 自学习排序** — 强化学习驱动，高价值记忆上浮，低价值自然衰减
- **租户级隔离** — 多团队多用户，URI 命名空间隔离
- **MCP Server** — 通过 FastMCP v3 暴露 6 个工具给外部 Agent

## 技术栈

- Python 3.10+, async-first
- 向量存储: RuVector (OpenViking 内置引擎)
- Embedding: 火山引擎 doubao-embedding-vision (1024 dim)
- MCP: PrefectHQ FastMCP v3 (`fastmcp>=3.0`)
- 测试: unittest (32 个用例全过)

## 关键目录

```
src/opencortex/           # 核心框架
  config.py               # CortexConfig (tenant/user/ruvector/mcp)
  orchestrator.py          # MemoryOrchestrator 顶层 API
  mcp_server.py            # FastMCP Server (6 tools)
  storage/
    vikingdb_interface.py  # 抽象接口 (25 async 方法)
    ruvector/adapter.py    # RuVectorAdapter (SONA 强化面)
  retrieve/
    hierarchical_retriever.py  # 层级递归检索
  models/embedder/         # 嵌入模型抽象

tests/
  test_e2e_phase1.py       # 24 个 E2E 测试
  test_mcp_server.py       # 8 个 MCP 测试

docs/architecture.md       # 架构设计文档
```

## 开发约定

- 所有存储操作通过 `VikingDBInterface` 抽象，方法均为 `async`
- URI 格式: `opencortex://tenant/{team}/user/{uid}/{type}/{category}/{node_id}`
- 配置优先从 `opencortex.json` 加载
- SONA 方法 (update_reward/get_profile/apply_decay/set_protected) 不在接口中，通过 `hasattr` 检测
- 运行测试: `PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server -v`

## MCP Server

```bash
# stdio (本地)
python -m opencortex.mcp_server --config opencortex.json

# SSE (远程)
python -m opencortex.mcp_server --transport sse --port 8920
```

Tools: `memory_store`, `memory_search`, `memory_feedback`, `memory_stats`, `memory_decay`, `memory_health`

## 当前状态

已完成: 核心框架 + MCP Server + 32 测试
待实现: 真实 Embedding 接入, VLM/LLM 接入, 本地事件采集, 远程同步
