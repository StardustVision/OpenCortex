# OpenCortex Data Flow — Claude Code Session Lifecycle

## Architecture Overview

```
Claude Code (Agent)
  │
  ├── Hooks (Node.js, lifecycle events)
  │     session-start → user-prompt-submit → stop (per turn) → session-end
  │
  ├── MCP Server (Node.js stdio proxy, 25 tools)
  │     memory_store / memory_search / memory_feedback / session_* / hooks_*
  │
  └──── HTTP fetch + X-* Headers ────┐
                                     ▼
                              FastAPI HTTP Server (Python)
                                     │
                        RequestContextMiddleware
                        (X-Tenant-ID, X-User-ID, X-Project-ID → contextvars)
                                     │
                              MemoryOrchestrator
                              ┌──────┼──────┐
                              ▼      ▼      ▼
                          CortexFS  Qdrant  IntentRouter
                          (L0/L1/L2) (vector) (query→intent)
```

**Single Collection**: `context` (Qdrant embedded)
**Dual-Write**: 每条记忆同时写入 Qdrant (向量+payload) 和 CortexFS (三层文件)

---

## Phase 1: Session Start

**触发**: Claude Code 打开会话
**Handler**: `plugins/opencortex-memory/hooks/handlers/session-start.mjs`

```
┌─────────────────────────────────────────────────────────────┐
│  Claude Code fires SessionStart                             │
│  ↓                                                          │
│  1. ensureDefaultConfig()                                   │
│     - 查找 mcp.json (项目根目录 or ~/.opencortex/)          │
│     - 读取 tenant_id, user_id, port, share_skills 等       │
│                                                             │
│  2. healthCheck()                                           │
│     POST /api/v1/memory/health                              │
│     Headers: X-Tenant-ID, X-User-ID, X-Project-ID          │
│     ↓                                                       │
│     如果未运行 + mode=local:                                 │
│       spawn: uv run opencortex-server --port {port}         │
│       等待最多 10s 直到 health 返回 200                      │
│                                                             │
│  3. 写入 .opencortex/memory/session_state.json              │
│     {                                                       │
│       "active": true,                                       │
│       "mode": "local",                                      │
│       "http_url": "http://127.0.0.1:8921",                  │
│       "tenant_id": "netops",                                │
│       "user_id": "liaowh4",                                 │
│       "http_pid": 12345,                                    │
│       "ingested_turns": 0,                                  │
│       "started_at": 1709420000                              │
│     }                                                       │
│                                                             │
│  4. 返回 system message: "Memory system active"             │
└─────────────────────────────────────────────────────────────┘
```

**集合操作**: 无（仅 health check）

---

## Phase 2: User Prompt Submit

**触发**: 用户发送消息
**Handler**: `plugins/opencortex-memory/hooks/handlers/user-prompt-submit.mjs`

```
┌─────────────────────────────────────────────────────────────┐
│  用户输入: "FleetDM 查询超时怎么办"                          │
│  ↓                                                          │
│  1. 读取 session_state.json                                 │
│                                                             │
│  2. Intent 分析 (可选, 2s 超时)                              │
│     POST /api/v1/intent/should_recall                       │
│     Body: { "query": "FleetDM 查询超时怎么办" }             │
│     ↓                                                       │
│     IntentRouter 三层分析:                                   │
│       L1: 关键词匹配 (毫秒级)                               │
│           - 检测无召回模式: "你好","再见","好的" → 跳过       │
│           - 检测硬关键词: CamelCase/ALL_CAPS → 提升 lexical  │
│       L2: LLM 语义分类 (如果可用)                           │
│           → intent_type, top_k, detail_level, should_recall │
│       L3: Memory Trigger → trigger_categories               │
│     ↓                                                       │
│     返回: { "should_recall": true }                          │
│                                                             │
│  3. 决策:                                                   │
│     should_recall=true → 注入 system message:               │
│       "请先调用 memory_search 检查相关记忆"                  │
│     should_recall=false → 返回空（不注入）                    │
└─────────────────────────────────────────────────────────────┘
```

**集合操作**: 无（仅意图判断）

---

## Phase 3: MCP Tool Calls (交互期间)

### 3a. memory_store — 存储记忆

**触发**: Agent 调用 `memory_store` MCP tool
**路由**: MCP Server → `POST /api/v1/memory/store` → `orchestrator.add()`

```
┌─────────────────────────────────────────────────────────────┐
│  MCP Tool Call:                                             │
│  memory_store({                                             │
│    abstract: "FleetDM 超时根因是 context canceled",          │
│    content: "详细排查过程...",                               │
│    category: "cases",                                       │
│    context_type: "case"                                     │
│  })                                                         │
│  ↓                                                          │
│  HTTP Client (lib/http-client.mjs):                         │
│    buildClientHeaders() 从 mcp.json 附加:                   │
│    ┌──────────────────────────────────────────┐              │
│    │ X-Tenant-ID: netops                     │              │
│    │ X-User-ID: liaowh4                      │              │
│    │ X-Project-ID: sase-agent                │              │
│    │ X-Share-Skills-To-Team: false           │              │
│    └──────────────────────────────────────────┘              │
│  ↓                                                          │
│  RequestContextMiddleware:                                   │
│    Headers → contextvars (per-request identity)             │
│  ↓                                                          │
│  orchestrator.add():                                        │
│                                                             │
│  ① URI 生成 (_auto_uri):                                    │
│     context_type="case" →                                   │
│       opencortex://netops/shared/cases/bfb38755             │
│     context_type="memory", category="events" →              │
│       opencortex://netops/user/liaowh4/memories/events/abc  │
│                                                             │
│  ② L0 质量增强 (_enrich_abstract):                          │
│     从 content 提取关键词补充到 abstract                     │
│                                                             │
│  ③ L1 Overview 生成:                                        │
│     content > 500 chars + LLM → LLM 摘要                   │
│     content ≤ 500 chars → 直接用 content                    │
│     无 LLM → 截断前 500 chars                               │
│                                                             │
│  ④ Embedding (run_in_executor, 2s 超时):                    │
│     embedder.embed(abstract) → {                            │
│       dense_vector: float[1024],                            │
│       sparse_vector: {indices: [], values: []}   # BM25    │
│     }                                                       │
│                                                             │
│  ⑤ 写入 Qdrant (context collection):                       │
│     ┌──────────────────────────────────────────┐             │
│     │ id: "uuid"                               │             │
│     │ uri: "opencortex://netops/shared/cases/…"│             │
│     │ vector: [1024 dim dense]                 │             │
│     │ sparse_vector: {BM25}                    │             │
│     │ abstract: "FleetDM 超时根因..."          │             │
│     │ overview: "L1 摘要"                      │             │
│     │ context_type: "case"                     │             │
│     │ category: "cases"                        │             │
│     │ scope: "shared"                          │             │
│     │ source_user_id: "liaowh4"                │             │
│     │ source_tenant_id: "netops"               │             │
│     │ project_id: "sase-agent"                 │             │
│     │ mergeable: false                         │             │
│     │ reward_score: 0.0                        │             │
│     │ active_count: 0                          │             │
│     │ accessed_at: "2026-03-03T..."            │             │
│     └──────────────────────────────────────────┘             │
│                                                             │
│  ⑥ 写入 CortexFS (三层文件):                               │
│     data_root/netops/shared/cases/bfb38755/                 │
│       ├── .abstract.md    (L0)                              │
│       ├── .overview.md    (L1)                              │
│       └── content.md      (L2)                              │
│                                                             │
│  → 返回: { uri, context_type, category, abstract }          │
└─────────────────────────────────────────────────────────────┘
```

### 3b. memory_search — 搜索记忆

**触发**: Agent 调用 `memory_search` MCP tool
**路由**: MCP Server → `POST /api/v1/memory/search` → `orchestrator.search()`

```
┌─────────────────────────────────────────────────────────────┐
│  MCP Tool Call:                                             │
│  memory_search({ query: "FleetDM 查询超时", limit: 5 })    │
│  ↓                                                          │
│  orchestrator.search():                                     │
│                                                             │
│  ① IntentRouter.route(query):                               │
│     → SearchIntent {                                        │
│         should_recall: true,                                │
│         intent_type: "recent_recall",                       │
│         top_k: 5,                                           │
│         detail_level: L1,                                   │
│         lexical_boost: 0.55,  // 检测到 "FleetDM" 硬关键词 │
│         queries: [TypedQuery(context_type=ANY)]             │
│       }                                                     │
│                                                             │
│  ② 构建过滤器 (scope + tenant + project 隔离):              │
│     ┌──────────────────────────────────────────┐             │
│     │ AND:                                     │             │
│     │   NOT context_type = "staging"           │             │
│     │   OR:                                    │             │
│     │     scope IN ["shared", ""]              │             │
│     │     AND:                                 │             │
│     │       scope = "private"                  │             │
│     │       source_user_id = "liaowh4"         │             │
│     │   source_tenant_id IN ["netops", ""]     │             │
│     │   project_id IN ["sase-agent","public",""]│            │
│     └──────────────────────────────────────────┘             │
│                                                             │
│  ③ HierarchicalRetriever.retrieve() (per TypedQuery):       │
│                                                             │
│     context_type=ANY → 不施加 context_type 过滤器           │
│                      → 搜索所有 root URI:                   │
│                        - /netops/user/liaowh4/memories/     │
│                        - /netops/shared/patterns/           │
│                        - /netops/shared/cases/              │
│                        - /netops/shared/skills/             │
│                        - /netops/resources/                 │
│                                                             │
│     并行搜索:                                               │
│     ┌──────────────┐  ┌──────────────┐                      │
│     │ Dense Search  │  │ Lexical Search│                     │
│     │ (向量相似度)  │  │ (BM25 全文)   │                     │
│     └──────┬───────┘  └──────┬───────┘                      │
│            └────────┬────────┘                               │
│                     ▼                                        │
│           RRF Fusion (Reciprocal Rank Fusion)               │
│           lexical_weight = 0.55 (硬关键词提升)              │
│                     ▼                                        │
│           Rerank (bge-reranker / LLM fallback)              │
│                     ▼                                        │
│           Score Fusion:                                      │
│           final = 0.7 × rerank + 0.3 × retrieval            │
│                 + 0.05 × reward + 0.03 × hotness            │
│                                                             │
│  ④ 异步更新访问统计 (fire-and-forget):                      │
│     对每个命中结果: active_count++ & accessed_at = now       │
│                                                             │
│  ⑤ 聚合结果 (_aggregate_results):                           │
│     按记录实际 context_type 分类:                            │
│       memory/case/pattern → memories[]                      │
│       resource            → resources[]                     │
│       skill               → skills[]                        │
│                                                             │
│  → 返回: { memories, resources, skills, total, search_intent}│
└─────────────────────────────────────────────────────────────┘
```

### 3c. memory_feedback — 强化学习反馈

```
┌─────────────────────────────────────────────────────────────┐
│  memory_feedback({ uri: "opencortex://...", reward: 1.0 })  │
│  ↓                                                          │
│  orchestrator.feedback(uri, reward):                        │
│    1. filter(context, uri) → 找到 record_id                │
│    2. storage.update_reward(context, record_id, reward)     │
│       → reward_score += reward                              │
│       → positive_feedback_count++ (if reward > 0)           │
│    3. active_count++ & accessed_at = now                    │
└─────────────────────────────────────────────────────────────┘
```

### 3d. hooks_remember / hooks_recall — 语义记忆 (重定向到 context)

```
┌─────────────────────────────────────────────────────────────┐
│  hooks_remember({ content: "部署前必须跑 lint", type: "workflows" })
│  ↓                                                          │
│  orchestrator.hooks_remember():                             │
│    → orchestrator.add(                                      │
│        abstract=content,                                    │
│        content=content,                                     │
│        category="workflows",                                │
│        context_type="memory"                                │
│      )                                                      │
│    → 存入 context 集合 (与 memory_store 同路径)             │
│                                                             │
│  hooks_recall({ query: "部署流程" })                         │
│  ↓                                                          │
│  orchestrator.hooks_recall():                               │
│    → orchestrator.search(query, limit)                      │
│    → 从 context 集合搜索 (与 memory_search 同路径)          │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 4: Stop Hook (每轮对话结束)

**触发**: Agent 停止生成（用户打断或完成回复）
**Handler**: `plugins/opencortex-memory/hooks/handlers/stop.mjs`

```
┌─────────────────────────────────────────────────────────────┐
│  Claude Code fires Stop event                               │
│  ↓                                                          │
│  1. 解析 JSONL Transcript:                                  │
│     每行: { "role":"user"|"assistant", "content":"...",     │
│             "uuid":"turn-xxx", ... }                        │
│                                                             │
│  2. extractLastTurn():                                      │
│     找最后一个 user message + 后续 assistant messages        │
│     提取 tool uses: "[tool-use] memory_search(...)"         │
│     → {                                                     │
│         turnUuid: "turn-xxx",                               │
│         userText: "用户输入前 200 chars",                    │
│         assistantText: "助手回复前 300 chars",               │
│         toolUses: ["[tool-use] memory_search(...)"]         │
│       }                                                     │
│                                                             │
│  3. 去重检查:                                               │
│     if turnUuid === state.last_turn_uuid → 跳过             │
│                                                             │
│  4. 存储为 session 记忆:                                    │
│     POST /api/v1/memory/store                               │
│     {                                                       │
│       abstract: "Session turn: User asked [前120chars]",    │
│       content: "User: [...]\nSummary: [...]\nAssistant:[…]",│
│       category: "session",                                  │
│       context_type: "memory",                               │
│       meta: {                                               │
│         turn_uuid: "turn-xxx",                              │
│         source: "hook:stop",                                │
│         timestamp: <now>                                    │
│       }                                                     │
│     }                                                       │
│                                                             │
│  5. 更新 session_state.json:                                │
│     ingested_turns++                                        │
│     last_turn_uuid = "turn-xxx"                             │
│     last_ingested_at = <now>                                │
└─────────────────────────────────────────────────────────────┘
```

**集合操作**: `context` (upsert 轮次记忆)

---

## Phase 5: Session End

**触发**: Claude Code 关闭会话
**Handler**: `plugins/opencortex-memory/hooks/handlers/session-end.mjs`

```
┌─────────────────────────────────────────────────────────────┐
│  Claude Code fires SessionEnd                               │
│  ↓                                                          │
│  1. 读取 session_state.json                                 │
│                                                             │
│  2. 存储 session 总结 (best-effort):                        │
│     POST /api/v1/memory/store                               │
│     {                                                       │
│       abstract: "Session summary: 8 turns",                 │
│       content: "Session with 8 turns, 10:00-11:30",         │
│       category: "session_summary",                          │
│       context_type: "memory",                               │
│       meta: {                                               │
│         source: "hook:session-end",                         │
│         ingested_turns: 8,                                  │
│         started_at: 1709420000,                             │
│         ended_at: 1709425400                                │
│       }                                                     │
│     }                                                       │
│                                                             │
│  3. 关闭 HTTP Server (如果 mode=local):                     │
│     process.kill(state.http_pid, 'SIGTERM')                 │
│                                                             │
│  4. 更新 state:                                             │
│     state.active = false                                    │
│     state.ended_at = <now>                                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 5b: Session End (MCP Tool — LLM 记忆提取)

**触发**: Agent 显式调用 `session_end` MCP tool
**路由**: `POST /api/v1/session/end` → `orchestrator.session_end()` → `SessionManager.end()`

```
┌─────────────────────────────────────────────────────────────┐
│  session_end({ session_id: "xxx", quality_score: 0.8 })     │
│  ↓                                                          │
│  SessionManager.end():                                      │
│                                                             │
│  ① MemoryExtractor.extract(messages, quality_score):        │
│     LLM Prompt:                                             │
│     "分析对话，提取以下类别记忆:                             │
│      - profile: 用户身份、角色、背景                        │
│      - preferences: 偏好、习惯、工作流                      │
│      - entities: 项目、路径、URL、配置                       │
│      - events: 决策、里程碑 (唯一, 不合并)                  │
│      - cases: 问题+解决方案 (唯一, 不合并)                  │
│      - patterns: 可复用最佳实践                             │
│      返回 JSON: [{abstract, content, category, confidence}]" │
│     ↓                                                       │
│     LLM 返回: [                                             │
│       { abstract: "用户偏好 dark theme",                    │
│         content: "...", category: "preferences",            │
│         confidence: 0.9 },                                  │
│       { abstract: "FleetDM 超时修复",                       │
│         content: "...", category: "cases",                  │
│         confidence: 0.8 },                                  │
│       ...                                                   │
│     ]                                                       │
│                                                             │
│  ② 语义去重 (仅 MERGEABLE 类别):                            │
│     MERGEABLE = {profile, preferences, entities, patterns}   │
│     NON-MERGEABLE = {events, cases}                          │
│                                                             │
│     For each extracted memory (confidence ≥ 0.3):           │
│       if category ∈ MERGEABLE:                              │
│         search(abstract, limit=3)                           │
│         if top_score ≥ 0.85:                                │
│           → MERGE: 追加内容到已有记忆                       │
│           → feedback(uri, +0.5) 正向强化                    │
│         else:                                               │
│           → CREATE NEW                                       │
│       else:  // events, cases                               │
│         → ALWAYS CREATE NEW                                  │
│                                                             │
│  ③ 存储:                                                    │
│     orchestrator.add(abstract, content, category, type)      │
│     → 写入 context 集合 (同 memory_store 路径)              │
│                                                             │
│  → 返回: {                                                  │
│      stored_count: 3,                                       │
│      merged_count: 1,                                       │
│      skipped_count: 2,  // confidence < 0.3                 │
│      quality_score: 0.8                                     │
│    }                                                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Qdrant Record Schema (context collection)

```
┌────────────────────────────────────────────────────────────┐
│                    context collection                      │
├────────────────────────────────────────────────────────────┤
│ id              : string (PK, UUID)                        │
│ uri             : path   (opencortex://tenant/...)         │
│ vector          : float[1024] (dense embedding)            │
│ sparse_vector   : sparse_vector (BM25 lexical)             │
│                                                            │
│ -- L0/L1 Payload (zero I/O search) --                      │
│ abstract        : string  (L0, vectorization text)         │
│ overview        : string  (L1, summary)                    │
│                                                            │
│ -- Type & Category --                                      │
│ context_type    : string  (memory/resource/skill/case/...) │
│ category        : string  (preferences/events/cases/...)   │
│ scope           : string  (private/shared)                 │
│                                                            │
│ -- Tenant Isolation --                                     │
│ source_user_id  : string                                   │
│ source_tenant_id: string                                   │
│ project_id      : string                                   │
│                                                            │
│ -- RL & Access --                                          │
│ reward_score            : float  (cumulative)              │
│ positive_feedback_count : int                              │
│ negative_feedback_count : int                              │
│ active_count            : int    (access frequency)        │
│ accessed_at             : datetime                         │
│ protected               : bool   (decay immunity)          │
│                                                            │
│ -- Metadata --                                             │
│ created_at      : datetime                                 │
│ updated_at      : datetime                                 │
│ parent_uri      : path                                     │
│ is_leaf         : bool                                     │
│ mergeable       : bool                                     │
│ session_id      : string                                   │
│ ttl_expires_at  : string  (staging only)                   │
└────────────────────────────────────────────────────────────┘
```

---

## URI Routing Table

| context_type | category           | URI Pattern                                          | scope   |
|--------------|--------------------|------------------------------------------------------|---------|
| memory       | profile            | `opencortex://{tid}/user/{uid}/memories/profile/{nid}` | private |
| memory       | preferences        | `opencortex://{tid}/user/{uid}/memories/preferences/{nid}` | private |
| memory       | entities           | `opencortex://{tid}/user/{uid}/memories/entities/{nid}` | private |
| memory       | events             | `opencortex://{tid}/user/{uid}/memories/events/{nid}` | private |
| memory       | session            | `opencortex://{tid}/user/{uid}/memories/events/{nid}` | private |
| case         | *                  | `opencortex://{tid}/shared/cases/{nid}`              | shared  |
| pattern      | *                  | `opencortex://{tid}/shared/patterns/{nid}`           | shared  |
| skill        | {section}          | `opencortex://{tid}/shared/skills/{section}/{nid}`   | shared  |
| resource     | {category}         | `opencortex://{tid}/resources/{project}/{cat}/{nid}` | shared  |
| staging      | *                  | `opencortex://{tid}/user/{uid}/staging/{nid}`        | private |

---

## Complete Lifecycle Sequence

```
Session Open
  │
  ▼
[Hook: session-start]
  │  - 加载 mcp.json
  │  - 启动 HTTP Server (local)
  │  - 写入 session_state.json
  │
  ▼
[Hook: user-prompt-submit]  ◄──────────────────┐
  │  - POST /intent/should_recall               │
  │  - 注入 system message (if should_recall)   │
  │                                              │
  ▼                                              │
[Agent 交互]                                     │
  │  ├── memory_search → Qdrant 查询             │
  │  │     (ANY type, scope+tenant+project 过滤) │
  │  │     (dense + lexical RRF + rerank)         │
  │  │                                            │
  │  ├── memory_store → Qdrant upsert + CortexFS │
  │  │     (embed → dual-write → URI 返回)       │
  │  │                                            │
  │  ├── memory_feedback → reward_score 更新      │
  │  │                                            │
  │  ├── hooks_remember → orchestrator.add()      │
  │  │     (重定向到 context 集合)                │
  │  │                                            │
  │  └── hooks_recall → orchestrator.search()     │
  │        (重定向到 context 集合)                │
  │                                              │
  ▼                                              │
[Hook: stop]                                     │
  │  - 解析 JSONL transcript                     │
  │  - 提取最后一轮 (user + assistant + tools)   │
  │  - 存储为 session 记忆 (category=session)    │
  │  - ingested_turns++                          │
  │                                              │
  └──────────── 下一轮 ─────────────────────────┘
  │
  ▼
[Hook: session-end]
  │  - 存储 session summary
  │  - kill HTTP Server (local)
  │  - state.active = false
  │
  ▼
[Optional: session_end tool]
  │  - LLM 记忆提取
  │  - 语义去重 (mergeable 0.85 阈值)
  │  - 批量存入 context 集合
  │
  ▼
Session Closed
```

---

## 关键参数速查

| 参数 | 值 | 位置 |
|------|-----|------|
| Embedding dim | 1024 | CortexConfig |
| Embed timeout | 2s (server) / 3s (client) | orchestrator / http-client |
| Rerank beta | 0.7 | HierarchicalRetriever |
| RL weight | 0.05 | HierarchicalRetriever |
| Hotness weight | 0.03 | HierarchicalRetriever |
| Dedup threshold | 0.85 | SessionManager |
| Confidence gate | 0.3 | MemoryExtractor |
| Lexical boost (default) | 0.3 | IntentRouter |
| Lexical boost (hard keywords) | 0.55 | IntentRouter |
| Max frontier size | 64 | HierarchicalRetriever |
| Max waves | 8 | HierarchicalRetriever |
| Chinese tokenizer | `[\u4e00-\u9fa5]` char-level | BM25SparseEmbedder |
