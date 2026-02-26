# OpenCortex MCP Server

通过 MCP (Model Context Protocol) 将 OpenCortex 记忆系统暴露给外部 AI Agent。基于 [PrefectHQ/FastMCP](https://github.com/PrefectHQ/fastmcp) v3 实现。

## 快速启动

```bash
# stdio 模式 (本地 Agent, 如 Claude Desktop)
python -m opencortex.mcp_server

# SSE 模式 (远程 Agent 通过 HTTP 连接)
python -m opencortex.mcp_server --transport sse --port 8920

# Streamable HTTP 模式
python -m opencortex.mcp_server --transport streamable-http --port 8920
```

### CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--transport` | `stdio` | 传输模式: `stdio` / `sse` / `streamable-http` |
| `--port` | `8920` | SSE/HTTP 监听端口 |
| `--host` | `127.0.0.1` | SSE/HTTP 监听地址 |
| `--config` | — | 指定 `opencortex.json` 配置文件路径 |
| `--log-level` | `INFO` | 日志级别: `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## MCP Tools

### memory_store

存储新的记忆、资源或技能。

**参数:**

| 名称 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `abstract` | string | 是 | 摘要（用于向量搜索） |
| `content` | string | 否 | 完整内容（L2 层存储） |
| `category` | string | 否 | 分类，如 `preferences`、`entities`、`patterns` |
| `context_type` | string | 否 | 类型: `memory` (默认) / `resource` / `skill` |
| `uri` | string | 否 | 显式 URI，不传则自动生成 |
| `meta` | object | 否 | 自定义元数据 |

**返回:**

```json
{
  "uri": "opencortex://tenant/default/user/default/memories/preferences/a1b2c3d4e5f6",
  "context_type": "memory",
  "category": "preferences",
  "abstract": "User prefers dark theme"
}
```

### memory_search

语义搜索已存储的记忆、资源和技能。

**参数:**

| 名称 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 自然语言查询 |
| `limit` | int | 否 | 最大返回数量 (默认 5) |
| `context_type` | string | 否 | 限定类型: `memory` / `resource` / `skill` |
| `category` | string | 否 | 按分类过滤 |

**返回:**

```json
{
  "results": [
    {
      "uri": "opencortex://...",
      "abstract": "User prefers dark theme in editors",
      "context_type": "memory",
      "score": 0.87
    }
  ],
  "total": 1
}
```

### memory_feedback

为记忆提交 SONA 强化学习奖励信号。正向奖励增强检索权重，负向奖励降低权重。

**参数:**

| 名称 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `uri` | string | 是 | 记忆的 URI |
| `reward` | float | 是 | 奖励值（正=有用，负=无用） |

**返回:**

```json
{"status": "ok", "uri": "opencortex://...", "reward": "1.0"}
```

### memory_stats

获取系统统计信息。

**返回:**

```json
{
  "tenant_id": "default",
  "user_id": "default",
  "storage": {
    "collections": 1,
    "total_records": 42,
    "storage_size": 0,
    "backend": "ruvector"
  },
  "embedder": "doubao-embedding-vision",
  "has_llm": false
}
```

### memory_decay

触发 SONA 时间衰减。不活跃的记忆有效分数逐渐降低（普通 0.95 衰减率，受保护 0.99 衰减率）。

**返回:**

```json
{
  "records_processed": 10,
  "records_decayed": 8,
  "records_below_threshold": 0,
  "records_archived": 0
}
```

### memory_health

检查所有组件的健康状态。

**返回:**

```json
{
  "initialized": true,
  "storage": true,
  "embedder": true,
  "llm": false
}
```

## 客户端连接

### Claude Desktop (stdio)

在 `claude_desktop_config.json` 中添加:

```json
{
  "mcpServers": {
    "opencortex": {
      "command": "python",
      "args": ["-m", "opencortex.mcp_server"],
      "env": {}
    }
  }
}
```

### Python Client (FastMCP)

```python
from fastmcp import Client

async def main():
    # stdio 连接
    async with Client("python -m opencortex.mcp_server") as client:
        # 存储
        result = await client.call_tool("memory_store", {
            "abstract": "User prefers dark theme",
            "category": "preferences",
        })

        # 搜索
        result = await client.call_tool("memory_search", {
            "query": "user theme preference",
        })

    # SSE 远程连接
    async with Client("http://localhost:8920/sse") as client:
        result = await client.call_tool("memory_health", {})
```

### 通用 MCP Client (任意语言)

SSE 模式下，任何支持 MCP 协议的客户端都可以连接:

```
SSE endpoint: http://{host}:{port}/sse
```

## 配置

`opencortex.json` 示例:

```json
{
  "tenant_id": "myteam",
  "user_id": "alice",
  "data_root": "./data",
  "embedding_dimension": 1024,
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision",
  "ruvector_host": "127.0.0.1",
  "ruvector_port": 6921,
  "mcp_transport": "stdio",
  "mcp_port": 8920
}
```

## 架构

```
External Agent
    │
    │  MCP Protocol (stdio / SSE / HTTP)
    ▼
┌─────────────────────────┐
│   FastMCP Server        │
│   6 tools registered    │
├─────────────────────────┤
│   MemoryOrchestrator    │
│   (lifespan managed)    │
├─────────────────────────┤
│   VikingFS  │  Embedder │
│   (L0/L1/L2)│  (1024d)  │
├─────────────────────────┤
│   RuVectorAdapter       │
│   (VikingDBInterface)   │
└─────────────────────────┘
```
