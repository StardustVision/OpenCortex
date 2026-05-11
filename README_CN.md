# OpenCortex

OpenCortex 是面向 AI Agent 的记忆存储与召回运行时。它把 memory、
resource、session 写入分层文件树和 Qdrant 向量索引，再通过 HTTP API、
Streamable HTTP MCP 和 Web 控制台提供访问。

当前仓库只保留新的 `opencortex` 运行时。旧记忆链路不再是活跃代码；insights、
autophagy、skill engine、自我升级等能力目前以详细设计文档形式放在
`docs/design/`。

## 当前能力

OpenCortex 当前提供：

- 用户事实、偏好、事件、工作流等 durable memory 写入。
- 文档和共享知识材料的 resource 写入。
- 对话 turn 和 session/end 的 session 写入。
- L0/L1/L2 三层存储：
  - `L0`：紧凑 abstract
  - `L1`：overview
  - `L2`：完整 content
- 面向召回的旁路索引：
  - primary object record
  - anchor index
  - fact index
  - entity index
  - reason-tree index
  - 为 cone expansion 准备的关系信号
- probe、planner、executor、ranker、reason-tree selection 和 cone expansion
  组成的召回链路。
- 按语义 query 或 URI 删除记忆。
- JWT 保护的 API、admin token 管理和 MCP 访问。
- React Web 控制台，用于 token 管理和记忆查看。

## 架构

```text
Client / Agent / MCP client
  -> Bearer JWT middleware
  -> FastAPI routes
     -> Store flows
        -> PrimaryRecordWriter
        -> CFS-backed CortexStorage
        -> QdrantVectorStore
        -> SQLite-backed persistent event queue
        -> semantic layer 和旁路索引后台 writer
     -> Retrieval flow
        -> probe
        -> planner
        -> executor
        -> ranker
        -> CFS hydration
  -> React console
```

核心边界：

- `storage/`：CFS、URI-tree 文件操作、CortexStorage、持久队列。
- `vector/`：Qdrant payload、向量存储、召回链路。
- `store/`：写入流程、session 流程、事件和 writer。
- `console/`：Web 控制台管理 API，不侵入 MCP 或公开 memory API。
- `mcp/`：通过 Streamable HTTP MCP 暴露同一套 memory 能力。

## 目录结构

```text
src/opencortex/
  app.py                  FastAPI app factory 和运行时装配
  settings.py             OPENCORTEX_APP_* 配置
  auth/                   JWT 生成、验证、admin token API
  console/                Web 控制台专用管理 API
  core/                   请求身份上下文和 middleware
  llm/                    OpenAI-compatible LLM client
  mcp/                    Streamable HTTP MCP transport 和 tools
  parse/                  文档 parser adapter
  prompts/                写入和召回 prompt
  storage/                CFS、CortexStorage、持久队列、URI namespace
  store/                  写入、session、event flow 和 writer
  vector/                 Qdrant store、payload schema、retrieval pipeline

web/                      React/Vite 控制台
tests/opencortex/         当前新运行时测试
docs/design/              详细功能设计和后续排期
```

## 环境要求

- Python `>=3.10`
- `uv`
- Node.js `>=18`，用于 Web 控制台
- Qdrant 可用 embedded local 模式；生产级或大数据量建议使用独立 Qdrant
  Server，并通过 `OPENCORTEX_APP_QDRANT_URL` 接入。

## 安装

```bash
uv sync
```

如需文档解析依赖：

```bash
uv sync --extra parsers
```

Web 控制台依赖：

```bash
cd web
npm install
```

## 配置

配置使用 `OPENCORTEX_APP_` 环境变量前缀，也可以放入 `.env`。

常用配置：

```bash
export OPENCORTEX_APP_DATA_ROOT=./data
export OPENCORTEX_APP_VECTOR_DIMENSION=1024

# 留空则使用 embedded local Qdrant。
export OPENCORTEX_APP_QDRANT_URL=
export OPENCORTEX_APP_QDRANT_API_KEY=

export OPENCORTEX_APP_EMBEDDING_API_BASE=https://api.openai.com/v1
export OPENCORTEX_APP_EMBEDDING_API_KEY=<embedding-key>
export OPENCORTEX_APP_EMBEDDING_MODEL=text-embedding-3-small

export OPENCORTEX_APP_LLM_API_BASE=https://api.openai.com/v1
export OPENCORTEX_APP_LLM_API_KEY=<llm-key>
export OPENCORTEX_APP_LLM_MODEL=gpt-4o-mini
export OPENCORTEX_APP_LLM_API_STYLE=openai

export OPENCORTEX_APP_STORE_EVENT_WORKER_CONCURRENCY=4
```

运行时会创建：

- `data/auth_secret.key`：JWT 签名密钥
- `data/tokens.json`：已签发 token 记录
- `data/qdrant/`：embedded Qdrant 数据
- CFS 内容树
- 持久事件队列

不要提交 `data*/`、日志、token 或本地密钥。

## 启动后端

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921
```

等价入口：

```bash
uv run opencortex --host 127.0.0.1 --port 8921
```

开发 reload：

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921 --reload
```

## 鉴权

所有 `/api/*`、`/admin/*`、`/console/*` 和 `/mcp` 都需要：

```http
Authorization: Bearer <jwt>
```

Token 表示租户和用户身份。`project` 是业务属性，不属于 API key 创建参数。

生成用户 token：

```bash
uv run opencortex-token generate
```

查看 token：

```bash
uv run opencortex-token list
```

按 prefix 撤销：

```bash
uv run opencortex-token revoke <token-prefix>
```

Admin token 管理走 `/admin/v1/tokens`。也可以通过配置注入 bootstrap admin token：

```bash
export OPENCORTEX_APP_ADMIN_API_TOKEN=<admin-jwt>
```

该 token 必须由当前 `data/auth_secret.key` 签名。

## HTTP API

### 写入 Memory 或 Resource

`POST /api/v1/memory/store`

Memory 示例：

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/store \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "memory",
    "content": "Alice prefers concise technical summaries.",
    "category": "semantic",
    "metadata": {"entities": ["Alice"]},
    "source": {"kind": "manual"}
  }'
```

Resource 示例：

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/store \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "resource",
    "content": "# Qdrant Notes\n\nUse server Qdrant for production-like runs.",
    "category": "semantic",
    "metadata": {"title": "Qdrant Notes", "source_path": "/docs/qdrant.md"},
    "source": {"kind": "document", "path": "/docs/qdrant.md", "title": "Qdrant Notes"}
  }'
```

同步请求只写 primary record。LLM 语义派生、L0/L1/L2 CFS 写入和旁路索引由后台
worker 处理。

### 召回

`POST /api/v1/memory/search`

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/search \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "What does Alice prefer?", "limit": 5}'
```

公开召回请求只接受：

- `query`
- `limit`

筛选和管理行为属于 console API，不放进公开 memory recall contract。

### 删除

`POST /api/v1/memory/forget`

语义删除：

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/forget \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "Alice concise summaries"}'
```

按 URI 删除：

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/forget \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"uri": "opencortex://tenant/user/memories/public/semantic/example"}'
```

### 写入 Session Message

`POST /api/v1/session/message`

```bash
curl -sS http://127.0.0.1:8921/api/v1/session/message \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session-001",
    "turn_id": "turn-001",
    "messages": [
      {"role": "user", "content": "Remember that Alice prefers concise summaries."}
    ],
    "tool_calls": [],
    "cited_uris": []
  }'
```

Session message 会写 immediate record，并在满足条件时触发 merge。merge 完成后会清理
旧 immediate records。

### 结束 Session

`POST /api/v1/session/end`

```bash
curl -sS http://127.0.0.1:8921/api/v1/session/end \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "session-001"}'
```

Session end 会写 final session memory；如果内容是结构化的，还会生成 final tree 和
reason-tree side index。

### 当前身份

`GET /api/v1/auth/me`

```bash
curl -sS http://127.0.0.1:8921/api/v1/auth/me \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN"
```

## Admin API

Admin API 与 user memory API、MCP 分离。

### Token 列表

`GET /admin/v1/tokens`

列表只返回公开字段，不返回完整 token。

### 创建 Token

`POST /admin/v1/tokens`

```bash
curl -sS http://127.0.0.1:8921/admin/v1/tokens \
  -H "Authorization: Bearer $OPENCORTEX_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "tenant-a", "user_id": "alice"}'
```

完整 token 只在创建时返回一次。

### 撤销 Token

`DELETE /admin/v1/tokens`

```bash
curl -sS -X DELETE http://127.0.0.1:8921/admin/v1/tokens \
  -H "Authorization: Bearer $OPENCORTEX_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token_prefix": "abcd1234"}'
```

## Console API

Web 控制台使用 `/console/v1/*`。这些是管理面 API，不改变公开 memory API 或 MCP
contract。

当前 console routes：

- `GET /console/v1/stats`
- `GET /console/v1/memories`
- `POST /console/v1/memories/search`
- `GET /console/v1/memories/content?uri=...`
- `DELETE /console/v1/memories`

Admin 可传 tenant/user 筛选；普通用户只能看到自身身份范围。

## Web 控制台

先启动后端，再启动前端：

```bash
cd web
npm install
npm run dev
```

打开：

```text
http://127.0.0.1:5173
```

Vite dev server 会代理：

- `/api`
- `/admin`
- `/console`

当前控制台包含：

- token 校验登录
- dashboard stats
- memory search/list/detail/delete
- admin token management

## MCP

OpenCortex 支持 Streamable HTTP MCP：

```text
POST /mcp
```

必需 headers：

```http
Authorization: Bearer <jwt>
Content-Type: application/json
Accept: application/json, text/event-stream
```

当前 MCP tools：

- `opencortex.search`
- `opencortex.store_memory`
- `opencortex.store_resource`
- `opencortex.forget`
- `opencortex.session_message`
- `opencortex.session_end`

`GET /mcp` 和 `DELETE /mcp` 当前返回 `405`。当前实现是无状态 Streamable HTTP
JSON-RPC，不是旧 HTTP+SSE transport。

## 存储与索引设计

OpenCortex 写入两个持久面：

### CFS

CFS 保存 URI tree 和文件层：

```text
opencortex://<tenant>/<user>/<bucket>/<project>/<category>/<node>/
  content.md
  .abstract.md
  .overview.md
  .abstract.json
```

`CortexStorage` 是 CFS 之上的 URI storage facade。

### Qdrant

Qdrant 保存可向量检索的 payload。主要 retrieval surfaces：

- `l0_object`：primary memory/resource/session object
- `directory`：payload-only URI ancestor record
- `anchor_index`：定位相关记忆的 anchor handle
- `fact_index`：从内容提取的 fact point
- `entity_index`：entity projection
- `reason_tree_index`：reason-tree node 和 summary

`vector/` 拥有 payload schema 和召回逻辑。`storage/` 不直接拥有 Qdrant 写入。

## 召回链路

```text
RetrievalRequest
  -> Probe
  -> Planner
  -> Executor
  -> Ranker
  -> Reason-tree selection
  -> Cone expansion
  -> CFS hydration
  -> RetrievalResponse
```

公开 API 保持输入简单，只接受 `query` 和 `limit`。内部 plan 决定 surface、budget、
weight、depth、是否使用 reason tree，以及是否做 cone expansion。

## 后台事件

Primary write 会把事件写入持久队列。Worker action 负责：

- LLM semantic derivation
- CFS layer 写入
- search index 写入
- entity index 写入
- reason-tree build 和 index 写入
- session merge 和 cleanup
- 为后续 mutation 预留的 check-update event

队列持久化在 data root 下，中断后可恢复。

## 开发检查

Python：

```bash
uv run --group dev ruff format --check src/opencortex tests/opencortex
uv run --group dev ruff check src/opencortex tests/opencortex
uv run --group dev pytest tests/opencortex -q
```

Web：

```bash
cd web
npm run build
```

## 设计文档

详细设计和后续排期：

- `docs/design/opencortex-functional-parity.md`
- `docs/design/opencortex-recall-design.md`
- `docs/design/insights-functional-detail.md`
- `docs/design/autophagy-functional-detail.md`
- `docs/design/skill-engine-functional-detail.md`
- `docs/design/self-upgrade-functional-detail.md`

## License

Apache-2.0

