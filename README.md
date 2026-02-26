# OpenCortex

AI Agent 的记忆与上下文管理系统，从 [OpenViking](https://github.com/volcengine/openviking) 移植并重构。

## 核心能力

- **三层摘要 (L0/L1/L2)** — 按需返回精度层级，节省 Token
- **SONA 自学习排序** — 强化学习驱动，高价值记忆上浮，低价值自然衰减
- **租户级隔离** — 多团队多用户，URI 命名空间隔离
- **MCP Server** — 通过 FastMCP v3 暴露 6 个工具给外部 Agent
- **Hooks 自动采集** — 每次对话自动记录到上下文记忆，无需手动操作

## 快速开始

### 1. 安装依赖

```bash
pip install fastmcp>=3.0 volcenginesdkarkruntime
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
  "mcp_transport": "stdio",
  "mcp_port": 8920
}
```

### 3. 启动 MCP Server

```bash
# stdio 模式 (Claude Code / Claude Desktop)
PYTHONPATH=src python -m opencortex.mcp_server --config opencortex.json

# SSE 模式 (远程 Agent)
PYTHONPATH=src python -m opencortex.mcp_server --transport sse --port 8920
```

### 4. Claude Code 集成

项目已包含 `.mcp.json`，Claude Code 打开项目目录时会自动发现 MCP Server。

Hooks 自动记忆采集也已配置在 `.claude-plugin/hooks/` 中：
- **SessionStart** — 初始化记忆会话
- **UserPromptSubmit** — 提示记忆可用
- **Stop** — 每轮对话自动解析 transcript、摘要、存储
- **SessionEnd** — 存储 session summary

## MCP Tools

| Tool | 说明 |
|------|------|
| `memory_store` | 存储新记忆（自动 embedding + URI 生成） |
| `memory_search` | 语义搜索相关记忆 |
| `memory_feedback` | 正/负反馈（SONA 强化学习） |
| `memory_stats` | 查看存储统计 |
| `memory_decay` | 触发时间衰减 |
| `memory_health` | 健康检查 |

## 项目结构

```
src/opencortex/
  config.py                # CortexConfig (tenant/user/ruvector/mcp)
  orchestrator.py          # MemoryOrchestrator 顶层 API
  mcp_server.py            # FastMCP Server (6 tools)
  storage/
    vikingdb_interface.py  # 抽象接口 (25 async 方法)
    ruvector/adapter.py    # RuVectorAdapter (SONA 强化面)
  retrieve/
    hierarchical_retriever.py  # 层级递归检索
  models/
    embedder/              # Embedding 模型抽象
    llm_factory.py         # LLM completion 工厂

.claude-plugin/
  hooks/                   # Claude Code hooks (自动记忆采集)
  scripts/oc_memory.py     # Hook Python bridge
  skills/                  # memory-recall / opencortex-mcp

tests/
  test_e2e_phase1.py       # 24 个 E2E 测试
  test_mcp_server.py       # 8 个 MCP 测试
```

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server -v
```

## 技术栈

- Python 3.10+, async-first
- 向量存储: RuVector (OpenViking 内置引擎)
- Embedding: 火山引擎 doubao-embedding-vision (1024 dim)
- MCP: PrefectHQ FastMCP v3
- 测试: unittest (32 个用例)

## URI 格式

```
opencortex://tenant/{team}/user/{uid}/{type}/{category}/{node_id}
```

租户级隔离确保不同 team/user 的记忆完全独立。

## License

Apache-2.0
