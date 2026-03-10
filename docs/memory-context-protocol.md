# Memory Context Protocol — 详细设计文档

> 版本: v1.2-draft
> 日期: 2026-03-10
> 状态: 设计阶段（v1.2 — 修复跨租户碰撞 + fallback/transcript 矛盾）

## 1. 动机

当前 OpenCortex 的 Agent 生命周期管理完全依赖 Claude Code hooks（`session-start`、`user-prompt-submit`、`stop`、`session-end`）。这带来三个问题：

1. **平台锁定** — hooks 是 Claude Code 特有机制，无法移植到 Cursor、Windsurf、自研 Agent 等平台
2. **触发粗糙** — `user-prompt-submit` 注入硬编码指令 `Call memory_search with the user's query`，不区分 intent type
3. **读写耦合** — 召回（低延迟同步）和录入（可异步容错）混在同一路径

**目标**：设计一个 **平台无关的 MCP 协议**，用单个 `memory_context` 工具 + 三阶段 phase 替代 4 个 hooks，实现：
- 任何 MCP 兼容的 Agent 平台都能接入
- 读路径（recall）和写路径（record）解耦
- 幂等重试安全
- 失败降级不阻塞 Agent

---

## 2. 协议概览

### 2.1 单工具、多 Phase

```
┌─────────────────────────────────────────────────────────┐
│                    memory_context                        │
│                                                         │
│  phase: "prepare" │ "commit" │ "end"                    │
│                                                         │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │ prepare  │──▶│  commit  │──▶│   end    │            │
│  │ (同步读) │   │ (异步写) │   │ (flush)  │            │
│  └──────────┘   └──────────┘   └──────────┘            │
│                                                         │
│  每轮: prepare → Agent 生成回答 → commit                │
│  结束: end                                              │
└─────────────────────────────────────────────────────────┘
```

### 2.2 时序图

```
Agent                          Server
  │                              │
  │── prepare(sid, tid, user_msg) ──▶│  1. auto-create session if needed (idempotent)
  │                              │  2. IntentRouter.route(query)
  │                              │  3. if should_recall: search()
  │                              │  4. knowledge_search()
  │◀── {memory, knowledge, instructions} ──│  5. cache result by (sid, tid)
  │                              │
  │   ... Agent generates response ...
  │                              │
  │── commit(sid, tid, msgs, cited) ──▶│  6. Observer.record_batch() (sync)
  │◀── {accepted: true} ─────────│  7. return immediately
  │                              │
  │   ... more turns ...         │
  │                              │
  │── end(sid) ─────────────────▶│  8. Observer.flush()
  │                              │  9. TraceSplitter + Archivist
  │◀── {traces, knowledge_candidates} ──│ 10. cleanup session state
  │                              │
```

### 2.3 与 Hook 模型的对应关系

| Hook | Phase | 差异 |
|------|-------|------|
| `session-start` | `prepare`（首次调用自动 begin） | 不再需要单独 begin |
| `user-prompt-submit` | `prepare` | 返回数据而非注入指令 |
| `stop` | `commit` | 客户端 fire-and-forget，服务端负责持久化 |
| `session-end` | `end` | 直接 flush，无需 drain queue |

---

## 3. API 规格

### 3.1 MCP Tool 定义

```json
{
  "name": "memory_context",
  "description": "Unified lifecycle tool for memory recall and session recording. Call with phase='prepare' before generating a response to get relevant context. Call with phase='commit' after generating a response to record the conversation turn. Call with phase='end' to close the session.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "session_id": {
        "type": "string",
        "pattern": "^[a-zA-Z0-9_-]{1,128}$",
        "description": "Session identifier. Must be provided by the client on every call."
      },
      "turn_id": {
        "type": "string",
        "pattern": "^[a-zA-Z0-9_-]{1,128}$",
        "description": "Unique turn identifier for idempotency. Required for prepare and commit. Use UUID or session_id + counter."
      },
      "phase": {
        "type": "string",
        "enum": ["prepare", "commit", "end"],
        "description": "Lifecycle phase."
      },
      "messages": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "role": { "type": "string", "enum": ["user", "assistant", "system"] },
            "content": { "type": "string" }
          },
          "required": ["role", "content"]
        },
        "description": "Messages for this turn. prepare: user message only. commit: full turn (user + assistant)."
      },
      "cited_uris": {
        "type": "array",
        "items": { "type": "string" },
        "description": "URIs of memory items actually referenced by Agent in its response. Optional, used for RL reward feedback. Only meaningful in commit phase."
      },
      "config": {
        "type": "object",
        "properties": {
          "max_items": {
            "type": "integer",
            "description": "Maximum recall items to return (1-20). Default: 5."
          },
          "detail_level": {
            "type": "string",
            "enum": ["l0", "l1", "l2"],
            "description": "Content depth: l0=abstract only, l1=abstract+overview, l2=full content. Default: l1."
          },
          "recall_mode": {
            "type": "string",
            "enum": ["auto", "always", "never"],
            "description": "auto: IntentRouter decides. always: force recall. never: skip. Default: auto."
          }
        },
        "description": "Optional per-call configuration. Only meaningful in prepare phase."
      }
    },
    "required": ["session_id", "phase"]
  }
}
```

### 3.2 Phase 必填字段约束

| 字段 | prepare | commit | end |
|------|---------|--------|-----|
| `session_id` | **必填** | **必填** | **必填** |
| `turn_id` | **必填** | **必填** | 忽略 |
| `messages` | **必填**（至少一条 role=user） | **必填**（至少 user + assistant） | 忽略 |
| `cited_uris` | 忽略 | 可选 | 忽略 |
| `config` | 可选 | 忽略 | 忽略 |

服务端对每个 phase 校验必填字段，不满足 → 返回 HTTP 422。

### 3.3 身份来源

**身份（tenant_id / user_id）只从 HTTP transport headers 获取**，不从 request body 传递：

```
POST /api/v1/context
X-Tenant-ID: netops          ← identity
X-User-ID: liaowh4           ← identity
X-Project-ID: OpenCortex     ← project scope
Content-Type: application/json

{                             ← body 中没有 identity 字段
  "session_id": "sess_abc123",
  "turn_id": "turn_001",
  "phase": "prepare",
  "messages": [...]
}
```

`RequestContextMiddleware` 解析 headers → contextvars → `get_effective_identity()` 提供给 ContextManager。MCP tool 调用经过 `mcp-server.mjs` HTTP proxy 时由 `buildClientHeaders()` 自动附加 identity headers。

### 3.4 Phase: prepare

**用途**：每轮开始前调用，获取上下文。

**输入**：
```json
{
  "session_id": "sess_abc123",
  "turn_id": "turn_001",
  "phase": "prepare",
  "messages": [
    {"role": "user", "content": "如何配置 Nginx 反向代理？"}
  ],
  "config": {
    "max_items": 5,
    "detail_level": "l1",
    "recall_mode": "auto"
  }
}
```

**服务端行为**：
1. 校验必填字段（session_id, turn_id, messages 至少一条 user）
2. 幂等检查：`(session_id, turn_id)` 命中缓存 → 直接返回（同时 touch session activity）
3. 如果 session 不存在 → 加 session 级锁 → 调用 `Observer.begin_session()`（幂等）
4. 从 `messages` 中提取用户查询（最后一条 role=user 的 content）
5. 调用 `IntentRouter.route(query)` → `SearchIntent`（2s timeout）
6. 如果 `should_recall=true`（或 `recall_mode=always`）：
   - 调用 `HierarchicalRetriever.search()` → 记忆结果（按 `max_items` 限制条数）
   - 调用 `KnowledgeStore.search()` → 知识结果
   - 每条结果按 `detail_level` 返回完整内容，不做截断（单条内容硬上限 50k 字符）
7. 缓存结果到 `_prepare_cache[(session_id, turn_id)]`（TTL 5min）
8. 返回

**输出**：
```json
{
  "session_id": "sess_abc123",
  "turn_id": "turn_001",
  "intent": {
    "should_recall": true,
    "intent_type": "quick_lookup",
    "detail_level": "l1"
  },
  "memory": [
    {
      "uri": "opencortex://netops/user/liaowh4/memory/entities/abc",
      "abstract": "Nginx 反向代理配置: proxy_pass + upstream 块",
      "overview": "核心配置包括 upstream 定义后端服务器池...",
      "score": 0.87,
      "context_type": "memory",
      "category": "entities"
    }
  ],
  "knowledge": [
    {
      "knowledge_id": "k_xyz",
      "type": "sop",
      "abstract": "Nginx 配置变更必须 nginx -t 验证后才能 reload",
      "confidence": 0.92
    }
  ],
  "instructions": {
    "should_cite_memory": true,
    "memory_confidence": 0.87,
    "recall_count": 3,
    "guidance": "Found relevant Nginx configuration experience. Consider referencing before answering."
  }
}
```

**幂等性**：同一 `(session_id, turn_id)` 重复调用 → 直接返回缓存结果，不重新搜索。缓存命中也会 touch `_session_activity` 延长 session 寿命。

**失败降级**：
```json
{
  "session_id": "sess_abc123",
  "turn_id": "turn_001",
  "intent": { "should_recall": false, "intent_type": "unknown" },
  "memory": [],
  "knowledge": [],
  "instructions": {
    "should_cite_memory": false,
    "memory_confidence": 0.0,
    "recall_count": 0,
    "guidance": "Memory recall unavailable. Proceed without context."
  },
  "_error": "IntentRouter timeout after 2000ms"
}
```

### 3.5 Phase: commit

**用途**：Agent 生成回答后调用，记录完整对话。

**语义**：**客户端 fire-and-forget，服务端负责持久化和补偿。** Agent 不需要检查 commit 结果，也不需要实现重试逻辑。如果 Observer 写入失败，服务端内部补偿（重试或降级记录到本地日志）。

**输入**：
```json
{
  "session_id": "sess_abc123",
  "turn_id": "turn_001",
  "phase": "commit",
  "messages": [
    {"role": "user", "content": "如何配置 Nginx 反向代理？"},
    {"role": "assistant", "content": "要配置 Nginx 反向代理，需要以下步骤..."}
  ],
  "cited_uris": [
    "opencortex://netops/user/liaowh4/memory/entities/abc"
  ]
}
```

**服务端行为**：
1. 校验必填字段（session_id, turn_id, messages 至少 user + assistant）
2. 幂等检查：`(session_id, turn_id)` 已 commit → 返回 `{accepted: true, write_status: "duplicate"}`
3. 调用 `Observer.record_batch(session_id, messages, tenant_id, user_id)`（同步写入内存 buffer）
4. 如果 `cited_uris` 非空 → 异步提交 RL reward（`+0.1` per cited URI）
5. 标记 turn_id 已提交，更新 session activity
6. 立即返回

**输出**：
```json
{
  "accepted": true,
  "write_status": "ok",
  "turn_id": "turn_001",
  "session_turns": 5
}
```

**失败**（服务端内部处理，客户端无需关心）：
- Observer 写入异常 → 记录到本地 fallback 日志（`{data_root}/commit_fallback.jsonl`），返回 `write_status: "fallback"`
- RL reward 失败 → 静默忽略
- 返回给客户端仍为 `{accepted: true}`（best-effort 语义）

> **重要**：走 fallback 路径的 turn **不在 Observer buffer 中**，end 阶段的 `Observer.flush()` 不会包含它们。Fallback 日志需要独立恢复（手动重放或定期归档任务）。Transcript 保证是 **best-effort**：正常情况下完整，Observer 故障时可能缺失 fallback turns。

### 3.6 Phase: end

**用途**：会话结束时调用。

**输入**：
```json
{
  "session_id": "sess_abc123",
  "phase": "end"
}
```

**服务端行为**：
1. 校验 session_id
2. 调用 `Observer.flush(session_id)` → 获取完整 transcript（所有已 commit 的 messages 都已在 Observer buffer 中，无需额外 drain）
3. 调用 `TraceSplitter.split(transcript)` → traces
4. 保存 traces 到 `TraceStore`
5. 如果达到 Archivist 触发阈值 → 执行知识提取
6. 清理：prepare 缓存中该 session 的所有条目（通过反向索引）、committed turns、session identity
7. 返回

**为什么不需要 queue drain**：commit 阶段的 `Observer.record_batch()` 是同步写入内存 buffer 的，不经过异步队列。当 end 调用时，所有 Observer 成功接收的 messages 已经在 buffer 中，`flush()` 可以直接获取 transcript。

**Transcript 完整性保证**：**best-effort**。如果所有 commit 的 Observer 写入都成功，transcript 是完整的。如果某些 commit 走了 fallback 路径（Observer 写入失败），这些 turn 不在 transcript 中，需要从 `commit_fallback.jsonl` 独立恢复。返回的 `total_turns` 是 ContextManager 记录的提交总数（含 fallback），而 transcript 中的实际 turn 数可能更少。

**输出**：
```json
{
  "session_id": "sess_abc123",
  "status": "closed",
  "total_turns": 12,
  "traces": 3,
  "knowledge_candidates": 1,
  "duration_ms": 2450
}
```

**失败降级**：
```json
{
  "session_id": "sess_abc123",
  "status": "partial",
  "total_turns": 12,
  "traces": 0,
  "_error": "TraceSplitter timeout — transcript saved, will retry on next archivist trigger"
}
```

---

## 4. 服务端核心组件

### 4.0 session_key：跨租户隔离

**所有内部状态使用 `session_key = (tenant_id, user_id, session_id)` 作为键**，而非裸 `session_id`。

原因：`session_id` 由客户端生成，不同租户/用户可能碰巧生成相同的 session_id（如 `"sess_001"`）。如果用裸 session_id 索引内部状态，两个不同用户会共享缓存、幂等去重和 idle 清理，破坏隔离性。

```python
# Type alias
SessionKey = Tuple[str, str, str]  # (tenant_id, user_id, session_id)

def _make_session_key(self, tenant_id: str, user_id: str, session_id: str) -> SessionKey:
    return (tenant_id, user_id, session_id)
```

prepare 缓存键同理提升为 `(tenant_id, user_id, session_id, turn_id)`。

### 4.1 ContextManager

新增类，位于 `src/opencortex/context/manager.py`，封装三阶段逻辑：

```python
# Type aliases
SessionKey = Tuple[str, str, str]           # (tenant_id, user_id, session_id)
CacheKey = Tuple[str, str, str, str]        # (tenant_id, user_id, session_id, turn_id)

class ContextManager:
    """Manages the prepare/commit/end lifecycle for memory_context protocol."""

    def __init__(
        self,
        orchestrator: MemoryOrchestrator,
        intent_router: IntentRouter,
        observer: Observer,
        retriever: HierarchicalRetriever,
        knowledge_store: Optional[KnowledgeStore],
        *,
        prepare_cache_ttl: float = 300.0,          # 5 min
        session_idle_ttl: float = 1800.0,           # 30 min auto-close
        idle_check_interval: float = 60.0,          # configurable sweep interval
        max_content_chars: int = 50_000,             # per-item hard limit
    ):
        self._orchestrator = orchestrator
        self._intent_router = intent_router
        self._observer = observer
        self._retriever = retriever
        self._knowledge_store = knowledge_store

        # prepare 缓存: {(tid, uid, sid, turn_id): (result, timestamp)}
        self._prepare_cache: Dict[CacheKey, Tuple[Dict, float]] = {}

        # 反向索引: {session_key: set(cache_key)} — 用于 end 时清理
        self._session_cache_keys: Dict[SessionKey, Set[CacheKey]] = {}

        # 已提交 turn_id 集合: {session_key: set(turn_id)}
        self._committed_turns: Dict[SessionKey, Set[str]] = {}

        # session 活跃时间: {session_key: last_activity_timestamp}
        self._session_activity: Dict[SessionKey, float] = {}

        # session 级锁: 防止并发 prepare 重复 begin_session
        self._session_locks: Dict[SessionKey, asyncio.Lock] = {}

        # 后台 async tasks 跟踪（cited_uris reward 等）
        self._pending_tasks: Set[asyncio.Task] = set()

        # 配置
        self._prepare_cache_ttl = prepare_cache_ttl
        self._session_idle_ttl = session_idle_ttl
        self._idle_check_interval = idle_check_interval
        self._max_content_chars = max_content_chars

        # 后台任务
        self._idle_checker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动后台 worker（idle session 清理）。"""
        self._idle_checker = asyncio.create_task(self._idle_session_loop())

    async def close(self) -> None:
        """等待 pending tasks 完成，关闭 worker。"""
        if self._idle_checker:
            self._idle_checker.cancel()
        # 等待所有 pending async tasks（如 cited_uris reward）
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()
```

### 4.2 Phase 入口 (handle)

```python
async def handle(
    self,
    session_id: str,
    phase: str,
    tenant_id: str,
    user_id: str,
    turn_id: Optional[str] = None,
    messages: Optional[List[Dict[str, str]]] = None,
    cited_uris: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Unified entry point — dispatches to prepare/commit/end."""

    # Phase-specific validation
    if phase == "prepare":
        if not turn_id:
            raise ValueError("turn_id is required for prepare")
        if not messages or not any(m.get("role") == "user" for m in messages):
            raise ValueError("prepare requires at least one user message")
        return await self._prepare(session_id, turn_id, messages, tenant_id, user_id, config)

    elif phase == "commit":
        if not turn_id:
            raise ValueError("turn_id is required for commit")
        if not messages or len(messages) < 2:
            raise ValueError("commit requires at least user + assistant messages")
        return await self._commit(session_id, turn_id, messages, tenant_id, user_id, cited_uris)

    elif phase == "end":
        return await self._end(session_id, tenant_id, user_id)

    else:
        raise ValueError(f"Unknown phase: {phase}")
```

### 4.3 Prepare 内部流程

```python
async def _prepare(self, session_id, turn_id, messages, tenant_id, user_id, config=None):
    config = config or {}
    max_items = min(config.get("max_items", 5), 20)  # 服务端硬上限 20
    detail_level = config.get("detail_level", "l1")
    recall_mode = config.get("recall_mode", "auto")
    sk = self._make_session_key(tenant_id, user_id, session_id)

    # 1. 幂等：(tenant_id, user_id, session_id, turn_id) 缓存命中直接返回
    cache_key = (tenant_id, user_id, session_id, turn_id)
    cached = self._get_cached_prepare(cache_key)
    if cached is not None:
        self._touch_session(sk)  # 缓存命中也延长 session 寿命
        return cached

    # 2. Session auto-create（session 级锁防并发重复 begin）
    self._touch_session(sk)
    lock = self._session_locks.setdefault(sk, asyncio.Lock())
    async with lock:
        if not self._observer.has_session(session_id):
            self._observer.begin_session(session_id, tenant_id, user_id)

    # 3. 提取用户查询
    query = self._extract_query(messages)
    if not query:
        result = self._empty_prepare(session_id, turn_id)
        self._cache_prepare(cache_key, sk, result)
        return result

    # 4. Intent 分析
    intent = SearchIntent.default()
    if recall_mode != "never":
        try:
            intent = await asyncio.wait_for(
                self._intent_router.route(query),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[ContextManager] IntentRouter timeout for turn %s", turn_id)

    should_recall = (
        recall_mode == "always"
        or (recall_mode == "auto" and intent.should_recall)
    )

    # 5. 检索
    memory_items = []
    knowledge_items = []

    if should_recall:
        # 5a. Memory search
        try:
            find_result = await self._retriever.search(
                query=query,
                limit=max_items,
                detail_level=detail_level,
            )
            memory_items = self._format_memories(find_result, detail_level)
        except Exception as exc:
            logger.warning("[ContextManager] Memory search failed: %s", exc)

        # 5b. Knowledge search
        if self._knowledge_store:
            try:
                k_results = await self._knowledge_store.search(
                    query=query,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    limit=min(3, max_items),
                )
                knowledge_items = self._format_knowledge(k_results)
            except Exception as exc:
                logger.warning("[ContextManager] Knowledge search failed: %s", exc)

    # 6. 构建 instructions
    instructions = self._build_instructions(intent, memory_items, knowledge_items)

    result = {
        "session_id": session_id,
        "turn_id": turn_id,
        "intent": {
            "should_recall": should_recall,
            "intent_type": intent.intent_type,
            "detail_level": intent.detail_level.value if intent.detail_level else "l1",
        },
        "memory": memory_items,
        "knowledge": knowledge_items,
        "instructions": instructions,
    }

    self._cache_prepare(cache_key, sk, result)
    return result
```

### 4.4 Commit 内部流程

```python
async def _commit(self, session_id, turn_id, messages, tenant_id, user_id, cited_uris=None):
    sk = self._make_session_key(tenant_id, user_id, session_id)
    self._touch_session(sk)

    # 幂等检查（同 turn_id 第二次到达 → 忽略，即使 messages 不同也取第一次）
    if turn_id in self._committed_turns.get(sk, set()):
        return {
            "accepted": True,
            "write_status": "duplicate",
            "turn_id": turn_id,
        }

    # 写入 Observer（同步写入内存 buffer）
    observer_ok = True
    try:
        self._observer.record_batch(session_id, messages, tenant_id, user_id)
    except Exception as exc:
        observer_ok = False
        logger.warning("[ContextManager] Observer record failed: %s — writing to fallback", exc)
        self._write_fallback(session_id, turn_id, messages, tenant_id, user_id)

    # 标记已提交
    self._committed_turns.setdefault(sk, set()).add(turn_id)

    # RL reward 反馈（异步，不阻塞返回）
    if cited_uris:
        valid_uris = [u for u in cited_uris if u.startswith("opencortex://")]
        if valid_uris:
            task = asyncio.create_task(self._apply_cited_rewards(valid_uris))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

    return {
        "accepted": True,
        "write_status": "ok" if observer_ok else "fallback",
        "turn_id": turn_id,
        "session_turns": len(self._committed_turns.get(sk, set())),
    }

async def _apply_cited_rewards(self, uris: List[str]) -> None:
    """Apply +0.1 RL reward to each cited memory URI."""
    for uri in uris:
        try:
            await self._orchestrator.feedback(uri=uri, reward=0.1)
        except Exception as exc:
            logger.debug("[ContextManager] Reward feedback failed for %s: %s", uri, exc)
```

### 4.5 End 内部流程

```python
async def _end(self, session_id, tenant_id, user_id):
    sk = self._make_session_key(tenant_id, user_id, session_id)

    # 1. Observer.record_batch() in commit is synchronous — all *successfully*
    #    committed messages are in Observer's in-memory buffer.
    #    Fallback turns (Observer failures) are NOT in the buffer — they exist
    #    only in commit_fallback.jsonl and require separate recovery.
    #    No queue drain needed.

    # 2. 委托给 Orchestrator.session_end()
    #    (包含 Observer.flush → TraceSplitter → TraceStore → Archivist)
    result = await self._orchestrator.session_end(
        session_id=session_id,
        quality_score=0.5,
    )

    # 3. 清理 session 状态
    total_turns = len(self._committed_turns.get(sk, set()))
    self._cleanup_session(sk)

    return {
        "session_id": session_id,
        "status": "closed",
        "total_turns": total_turns,
        "traces": result.get("alpha_traces", 0),
        "knowledge_candidates": result.get("knowledge_candidates", 0),
        "duration_ms": result.get("trace_storage_duration_ms", 0),
    }

def _cleanup_session(self, sk: SessionKey) -> None:
    """Remove all session state including cache entries via reverse index."""
    # 通过反向索引清理 prepare cache
    cache_keys = self._session_cache_keys.pop(sk, set())
    for key in cache_keys:
        self._prepare_cache.pop(key, None)

    self._committed_turns.pop(sk, None)
    self._session_activity.pop(sk, None)
    self._session_locks.pop(sk, None)
```

### 4.6 缓存管理

```python
def _cache_prepare(self, cache_key: CacheKey, sk: SessionKey, result: Dict) -> None:
    """Cache prepare result with reverse index for session cleanup."""
    now = time.time()

    # LRU eviction: 超过 1000 条时淘汰最旧的
    if len(self._prepare_cache) >= 1000:
        oldest_key = min(self._prepare_cache, key=lambda k: self._prepare_cache[k][1])
        self._prepare_cache.pop(oldest_key)
        # 从反向索引中也移除
        for s, keys in self._session_cache_keys.items():
            keys.discard(oldest_key)

    self._prepare_cache[cache_key] = (result, now)
    self._session_cache_keys.setdefault(sk, set()).add(cache_key)

def _get_cached_prepare(self, cache_key: CacheKey) -> Optional[Dict]:
    """Return cached result if exists and not expired."""
    entry = self._prepare_cache.get(cache_key)
    if entry is None:
        return None
    result, ts = entry
    if time.time() - ts > self._prepare_cache_ttl:
        self._prepare_cache.pop(cache_key, None)
        return None
    return result
```

### 4.7 Session 空闲自动关闭

```python
async def _idle_session_loop(self):
    """Periodic sweep to auto-close idle sessions."""
    while True:
        await asyncio.sleep(self._idle_check_interval)  # configurable
        now = time.time()
        expired = [
            sk for sk, ts in self._session_activity.items()
            if now - ts > self._session_idle_ttl
        ]
        for sk in expired:
            tid, uid, sid = sk
            logger.info("[ContextManager] Auto-closing idle session %s (tenant=%s, user=%s)", sid, tid, uid)
            try:
                await self._end(sid, tid, uid)
            except Exception as exc:
                logger.warning("[ContextManager] Auto-close failed for %s: %s", sid, exc)
```

---

## 5. Instructions 生成策略

`instructions` 字段告诉 Agent 如何使用返回的上下文，根据 intent_type 差异化：

```python
def _build_instructions(self, intent, memory_items, knowledge_items):
    total_items = len(memory_items) + len(knowledge_items)

    if total_items == 0:
        return {
            "should_cite_memory": False,
            "memory_confidence": 0.0,
            "recall_count": 0,
            "guidance": "",
        }

    avg_score = sum(m.get("score", 0) for m in memory_items) / max(len(memory_items), 1)
    max_confidence = max(
        [k.get("confidence", 0) for k in knowledge_items],
        default=0.0,
    )
    confidence = max(avg_score, max_confidence)

    guidance_map = {
        "quick_lookup": "Relevant context found. Reference if directly applicable.",
        "deep_analysis": "Multiple related memories retrieved. Synthesize with retrieved context for comprehensive analysis.",
        "recent_recall": "Recent session context retrieved. Continue from where the conversation left off.",
        "summarize": "Historical context loaded. Summarize key themes and patterns.",
        "personalized": "User preferences and past patterns retrieved. Adapt response accordingly.",
    }
    guidance = guidance_map.get(intent.intent_type, "Context available for reference.")

    return {
        "should_cite_memory": confidence >= 0.5,
        "memory_confidence": round(confidence, 3),
        "recall_count": total_items,
        "guidance": guidance,
    }
```

---

## 6. 内容返回策略

**原则：不截断，全量返回。** 服务端不做 token 预算截断。截断意味着服务端替 Agent 决定哪些上下文重要，但服务端缺乏当前任务的完整语境，截断可能丢掉对 Agent 最关键的那条记忆。

Agent 自身有上下文窗口管理能力，应由 Agent 决定如何使用返回的内容。

**控制手段**（Agent 侧）：
- `max_items`: 控制返回条数（默认 5，服务端硬上限 20）
- `detail_level`: 控制每条记忆的内容深度
  - `l0`: 只返回 abstract（一行摘要，最轻量）
  - `l1`: 返回 abstract + overview（段落级摘要）
  - `l2`: 返回 abstract + overview + content（完整内容）

**服务端兜底**：单条内容字段硬上限 **50,000 字符**（约 25k tokens）。超出部分在末尾截断并附加 `"...[truncated]"` 标记。这是防御性措施，正常业务数据不应触发。

```python
def _format_memories(self, find_result, detail_level):
    """按 score 排序，返回完整内容。单条内容硬上限 50k 字符。"""
    items = []
    for matched in find_result:  # 已按 score 降序
        item = {
            "uri": matched.uri,
            "abstract": matched.abstract,
            "score": round(matched.score, 3),
            "context_type": str(matched.context_type),
            "category": matched.category,
        }
        if detail_level in ("l1", "l2") and matched.overview:
            item["overview"] = self._clamp(matched.overview)
        if detail_level == "l2" and matched.content:
            item["content"] = self._clamp(matched.content)
        items.append(item)
    return items

def _clamp(self, text: str) -> str:
    """Hard limit per-item content to max_content_chars."""
    if len(text) <= self._max_content_chars:
        return text
    return text[:self._max_content_chars] + "...[truncated]"
```

如果 Agent 上下文窗口较小（如 8k token 模型），可以：
1. 设置 `max_items: 2` + `detail_level: "l0"` 获取极简上下文
2. 对感兴趣的条目，后续用 `memory_search` 或 content API 按需加载 L2

---

## 7. 与现有架构的集成

### 7.1 不破坏现有 API

`memory_context` 是 **新增** endpoint，现有 API 全部保留：

```
# 新增
POST /api/v1/context          → ContextManager.handle()

# 保留（向后兼容）
POST /api/v1/session/begin    → Orchestrator.session_begin()
POST /api/v1/session/message  → Orchestrator.session_message()
POST /api/v1/session/end      → Orchestrator.session_end()
POST /api/v1/memory/search    → Orchestrator.search()
POST /api/v1/intent/should_recall → IntentRouter.route()
```

Hook-based Claude Code 插件继续工作。新的 `memory_context` MCP tool 是并行入口。

### 7.2 Orchestrator 变更

`MemoryOrchestrator` 新增 `_context_manager` 属性：

```python
# orchestrator.py init() 中
self._context_manager = ContextManager(
    orchestrator=self,
    intent_router=self._intent_router,
    observer=self._observer,
    retriever=self._retriever,
    knowledge_store=self._knowledge_store,
)
await self._context_manager.start()
```

### 7.3 HTTP Route

```python
# server.py
@app.post("/api/v1/context")
async def context_handler(req: ContextRequest) -> Dict[str, Any]:
    tid, uid = get_effective_identity()
    return await _orchestrator._context_manager.handle(
        session_id=req.session_id,
        phase=req.phase,
        tenant_id=tid,
        user_id=uid,
        turn_id=req.turn_id,
        messages=[m.model_dump() for m in req.messages] if req.messages else None,
        cited_uris=req.cited_uris,
        config=req.config.model_dump() if req.config else None,
    )
```

### 7.4 MCP Server 变更

`mcp-server.mjs` 新增 `memory_context` 工具定义，代理到 `POST /api/v1/context`。

现有 9 个工具保留。新工具是第 10 个。

---

## 8. MCP Tool 使用指南（Agent System Prompt）

以下指令写入 Agent 的 system prompt 或 CLAUDE.md：

```markdown
## Memory Context Protocol

You have access to the `memory_context` tool for persistent memory.

### Setup
Generate a `session_id` at the start of each session (e.g., timestamp + random suffix).
Use it consistently for all calls in this session.

### Every turn:
1. **Before responding**: Call `memory_context` with `phase: "prepare"`,
   your `session_id`, a unique `turn_id`, and the user's message.
   If results are returned, consider them when forming your response.
2. **After responding**: Call `memory_context` with `phase: "commit"`,
   the same `session_id` and `turn_id`, and the full turn messages.
   Optionally include `cited_uris` with URIs of memories you referenced.
   This is fire-and-forget — do not check the result.

### End of session:
Call `memory_context` with `phase: "end"` and your `session_id`.

### Example:
```json
// Step 1: prepare (before responding)
{
  "session_id": "sess_20260310_a1b2c3",
  "turn_id": "sess_20260310_a1b2c3_001",
  "phase": "prepare",
  "messages": [{"role": "user", "content": "How to configure Nginx?"}]
}

// Step 2: commit (after responding)
{
  "session_id": "sess_20260310_a1b2c3",
  "turn_id": "sess_20260310_a1b2c3_001",
  "phase": "commit",
  "messages": [
    {"role": "user", "content": "How to configure Nginx?"},
    {"role": "assistant", "content": "To configure Nginx reverse proxy..."}
  ],
  "cited_uris": ["opencortex://netops/user/liaowh4/memory/entities/abc"]
}

// Step 3: end session
{
  "session_id": "sess_20260310_a1b2c3",
  "phase": "end"
}
```

### Rules:
- Always provide `session_id` (generate once per session, reuse for all calls).
- Always provide a unique `turn_id` for prepare and commit (use `{session_id}_{counter}`).
- If prepare fails or returns empty, proceed normally — memory is optional.
- commit is fire-and-forget — do not inspect or retry based on its result.
```

---

## 9. 失败降级矩阵

| 组件故障 | prepare 行为 | commit 行为 | end 行为 |
|---------|------------|-----------|---------|
| IntentRouter 超时 | 返回空 memory + guidance="unavailable" | 不受影响 | 不受影响 |
| HierarchicalRetriever 失败 | 返回空 memory | 不受影响 | 不受影响 |
| KnowledgeStore 失败 | 返回空 knowledge | 不受影响 | 不受影响 |
| Observer 不可用 | 正常返回 | 写入 fallback 日志，返回 `write_status: "fallback"` | transcript 缺失 fallback turns，需独立恢复 |
| TraceSplitter 超时 | 不受影响 | 不受影响 | transcript 已保存，下次 Archivist 触发时重试 |
| Qdrant 宕机 | 全部返回空 | Observer 内存记录不受影响 | flush 正常，trace 保存失败 |

**核心原则**：
- prepare 失败 → Agent 继续回答（无 memory 不阻塞）
- commit 失败 → 写入 fallback log，Agent 侧仍为 accepted（best-effort transcript）
- end 失败 → Observer buffer 中的 transcript 仍在内存中，可手动触发 Archivist；fallback turns 需独立恢复

---

## 10. 与 Hook 方案的共存与迁移

### 10.1 共存期

两种方案可以同时运行：
- Claude Code 用户继续使用 hook（`user-prompt-submit` → inject instruction → Agent 调 `memory_search`）
- 其他平台用户使用 `memory_context` MCP tool

Observer 接受两种路径的输入，不冲突。

### 10.2 Claude Code 迁移路径

```
Phase 1: 发布 memory_context tool（与 hooks 并存）
Phase 2: 在 CLAUDE.md 中添加 memory_context 使用指南
Phase 3: 简化 hooks：
         - session-start: 只做 server health check
         - user-prompt-submit: 删除（Agent 自己调 prepare）
         - stop: 删除（Agent 自己调 commit）
         - session-end: 只调 memory_context(phase="end")
Phase 4: 完全移除 hooks（可选）
```

### 10.3 其他平台接入

任何支持 MCP 的平台只需：
1. 注册 `memory_context` tool
2. 在 system prompt 中添加 Section 8 的使用指南
3. 配置 HTTP headers（identity）

不需要 hooks、不需要 Node.js plugin、不需要 stdio proxy。纯 HTTP。

---

## 11. 数据模型

### 11.1 Pydantic Request Model

```python
# http/models.py
class ContextMessage(BaseModel):
    role: str
    content: str

class ContextConfig(BaseModel):
    max_items: int = Field(default=5, ge=1, le=20)
    detail_level: str = "l1"          # l0 | l1 | l2
    recall_mode: str = "auto"         # auto | always | never

class ContextRequest(BaseModel):
    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turn_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    phase: str  # prepare | commit | end
    messages: Optional[List[ContextMessage]] = None
    cited_uris: Optional[List[str]] = None
    config: Optional[ContextConfig] = None
```

`session_id` 为必填项。`turn_id` 在 prepare/commit 时由 `handle()` 校验必填。

### 11.2 内部缓存结构

```python
# Type aliases
SessionKey = Tuple[str, str, str]       # (tenant_id, user_id, session_id)
CacheKey = Tuple[str, str, str, str]    # (tenant_id, user_id, session_id, turn_id)

# ContextManager 内部状态

_prepare_cache: Dict[CacheKey, Tuple[Dict, float]]
# key: (tenant_id, user_id, session_id, turn_id) — 全限定复合键，杜绝跨租户/用户碰撞
# value: (prepare_result, timestamp)
# TTL: 5 min, LRU evict at 1000 entries

_session_cache_keys: Dict[SessionKey, Set[CacheKey]]
# key: (tenant_id, user_id, session_id)
# value: set of CacheKey — 反向索引用于 end 清理

_committed_turns: Dict[SessionKey, Set[str]]
# key: (tenant_id, user_id, session_id)
# value: set of committed turn_ids

_session_activity: Dict[SessionKey, float]
# key: (tenant_id, user_id, session_id)
# value: last activity timestamp (prepare/commit/end)

_session_locks: Dict[SessionKey, asyncio.Lock]
# key: (tenant_id, user_id, session_id)
# value: per-session lock to prevent concurrent begin_session

_pending_tasks: Set[asyncio.Task]
# 跟踪所有 create_task 创建的后台任务，close() 时 await 确保不丢数据
```

---

## 12. 实现计划

### Step 1: ContextManager 核心类
- 新建 `src/opencortex/context/__init__.py`
- 新建 `src/opencortex/context/manager.py`（ContextManager 类）
- 实现 handle → prepare / commit / end
- prepare 缓存（复合键 + 反向索引 + LRU）
- commit 幂等 + fallback log
- idle session auto-close

### Step 2: HTTP 路由 + 请求模型
- 在 `http/models.py` 添加 `ContextRequest` / `ContextMessage` / `ContextConfig`
- 在 `http/server.py` 添加 `POST /api/v1/context` 路由
- Orchestrator 初始化时创建 ContextManager

### Step 3: MCP Tool 定义
- 在 `mcp-server.mjs` 添加 `memory_context` tool（第 10 个）
- 代理到 `POST /api/v1/context`

### Step 4: 测试
- **基本流程**：prepare → commit → end 完整生命周期
- **幂等性**：同 `(session_id, turn_id)` 重复 prepare / commit
- **降级**：模拟 IntentRouter 超时、Observer 不可用、Qdrant 宕机
- **并发**：同一 session 并发两次 prepare（验证 session 级锁）
- **丢失补偿**：prepare 成功但 commit 未到达，直接 end（验证 transcript 完整性）
- **幂等冲突**：同 turn_id 的 commit 携带不同 messages（验证取第一次）
- **竞态**：end 与迟到的 commit 同时到达（验证 cleanup 不丢数据）
- **集成**：MCP tool → HTTP → ContextManager → 搜索 + Observer + RL reward

### Step 5: Claude Code 迁移（可选）
- 更新 CLAUDE.md 添加 memory_context 使用指南
- 简化 hooks（保留 session-start 做 health check + session-end 调 end）

---

## 13. 监控与可观测性

### 13.1 日志

```
INFO  [ContextManager] prepare sid=sess_abc tid=t1 intent=quick_lookup recall=3 latency=125ms
DEBUG [ContextManager] prepare sid=sess_abc tid=t1 CACHE_HIT
INFO  [ContextManager] commit sid=sess_abc tid=t1 messages=2 cited=1
DEBUG [ContextManager] commit sid=sess_abc tid=t1 DUPLICATE
WARN  [ContextManager] commit sid=sess_abc tid=t1 FALLBACK (observer error)
INFO  [ContextManager] end sid=sess_abc turns=12 traces=3 latency=2450ms
INFO  [ContextManager] idle-close sid=sess_abc after 1800s
```

日志级别规则：
- `DEBUG`: CACHE_HIT、DUPLICATE（高频正常路径，生产环境不输出）
- `INFO`: 正常 prepare/commit/end
- `WARN`: FALLBACK、observer 异常、idle-close 失败

### 13.2 Metrics（未来扩展）

```
context_prepare_latency_ms    — histogram
context_prepare_cache_hit     — counter (sampled)
context_commit_ok             — counter
context_commit_duplicate      — counter
context_commit_fallback       — counter
context_end_latency_ms        — histogram
context_session_auto_closed   — counter
context_cited_rewards_applied — counter
```

---

## 14. 安全考虑

1. **session_id / turn_id 格式约束**：正则 `^[a-zA-Z0-9_-]{1,128}$`，防止路径注入（CortexFS 涉及目录操作）
2. **session 隔离**：所有内部状态键为 `(tenant_id, user_id, session_id)` 三元组，不同用户即使生成相同 session_id 也完全隔离。搜索同样 scoped 到 tenant_id + user_id
3. **max_items 上限**：服务端强制 `max_items <= 20`，防止单次检索过重
4. **单条内容硬上限**：`max_content_chars = 50,000`（约 25k tokens），防止单条 L2 内容撑爆响应
5. **Rate limiting**：prepare 每 session 最多 100 次/分钟（防止重试风暴）
6. **Cache 内存上限**：prepare 缓存最多 1000 条，LRU 淘汰
7. **身份信源唯一**：identity 只从 HTTP headers 获取，body 中不传 tenant_id/user_id，避免权限绕过

---

## 附录 A：Review 反馈采纳记录

| # | 来源 | 反馈 | 处理 |
|---|------|------|------|
| G-A | Gemini | 保留物理硬上限防止上下文爆炸 | Section 6: 单条内容 50k 字符硬上限 |
| G-B | Gemini | cited_uris 反馈闭环 | Section 3.5: commit 新增 cited_uris → RL reward |
| G-C | Gemini | commit 重试退避 | 改为服务端补偿，客户端不重试 (Section 3.5) |
| G-D | Gemini | session_id pattern / cache touch / idle interval | Section 3.1, 4.3, 4.7: 全部采纳 |
| P-1 | GPT | turn_id 作用域 | Section 4.3/11.2: 缓存键改为 `(session_id, turn_id)` |
| P-2 | GPT | session_id 持有方 | Section 3.1/8: 强制客户端传入，不自动生成 |
| P-3 | GPT | commit 语义矛盾 | Section 3.5: 统一为"客户端 fire-and-forget，服务端补偿" |
| P-4 | GPT | queue.join 必要性 | Section 3.6: 移除 queue drain，补充状态机说明 |
| P-5 | GPT | phase 输入约束 | Section 3.2: 新增 per-phase 必填字段表 |
| P-6 | GPT | begin_session 并发 | Section 4.3: session 级 asyncio.Lock |
| P-7 | GPT | 缓存清理反向索引 | Section 4.5/4.6: `_session_cache_keys` 反向索引 |
| P-8 | GPT | 恢复 payload 兜底 | Section 6: 单条 50k 字符硬上限 |
| P-9 | GPT | identity 来源 | Section 3.3: 新增专门段落明确"只从 headers" |
| P-10 | GPT | 并发测试场景 | Section 12: 新增 4 类并发/竞态测试 |

### v1.2 采纳

| # | 来源 | 反馈 | 处理 |
|---|------|------|------|
| P-11 | GPT | session 内部状态跨租户碰撞 | Section 4.0: 引入 `SessionKey = (tid, uid, sid)` 三元组，所有内部状态键提升 |
| P-12 | GPT | fallback 与 transcript 完整性矛盾 | Section 3.5/3.6/9: 降级为 best-effort transcript，fallback turns 独立恢复 |
| G-E | Gemini | cited_uris 格式校验 | Section 4.4: `opencortex://` 前缀校验 |
| G-F | Gemini | async task 泄漏 | Section 4.1: `_pending_tasks` 集合 + `close()` 中 `asyncio.gather` |
| G-G | Gemini | 日志级别管理 | Section 13.1: CACHE_HIT/DUPLICATE → DEBUG，FALLBACK → WARN |
