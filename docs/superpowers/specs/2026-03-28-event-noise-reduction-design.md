# OpenCortex Event 噪声消除设计

## 概述

重构 `add_message` 工具，在数据源头将对话内容与工具执行数据做结构化分离，从根本上消除 `category="events"` 中的噪声，无需正则启发式。

### Events 生命周期

`events` 是**临时设计**——近期对话上下文的滑动窗口：

- **Immediate 层**：24h TTL，合并后删除或 session 结束时兜底清理
- **Merged 层**：168h（7 天）TTL，由 `cleanup_expired_staging()` 清理

目标是**在 7 天窗口内最大化 recall 精度**。含噪声的 events 会污染搜索结果、降低合并摘要质量、浪费 embedding 向量空间。

## 问题

当前 `add_message` 接受两个纯文本字段：`user_message` 和 `assistant_response`。LLM 自行组织 `assistant_response` 内容，经常将执行细节（工具输出、代码、diff、日志）与对话结论混在一起。服务端原样存储为 events——没有信号与噪声的结构化分离。

```
// 当前：所有内容混在一个字段
add_message({
  user_message: "修复右侧面板选择bug",
  assistant_response: "我读了 Memories.tsx 发现了问题...\n\nCommand: npm run build\nProcess exited with code 0\n\n修复方案是添加 cancelled flag 防止旧内容覆盖...",
  cited_uris: [...]
})
```

导致的结果：

- immediate 和 merged `events` 记录含大量噪声
- 合并摘要质量差（LLM 从含噪声的合并 buffer 派生 abstract）
- recall 质量下降（embedding 空间被执行语义污染）
- Memory Console 中显示大量低价值条目

## 方案：结构化三路分离

重构 `add_message`，接受三个独立字段：

```
add_message({
  user_message:       "修复右侧面板选择bug",
  assistant_response: "问题出在切换选中项时 content 状态未清空，导致旧数据残留。已添加 cancelled flag 修复竞态，并在选中变化时重置 content。",
  tool_calls:         [
    { name: "Read",  summary: "web/src/pages/Memories.tsx" },
    { name: "Edit",  summary: "修改 selectedMemory useEffect，添加 cancelled guard" },
    { name: "Bash",  summary: "npm run build — exit 0" }
  ],
  cited_uris:         [...]
})
```

| 字段 | 内容 | 存入 events 正文 | 存入 Observer |
|------|------|-----------------|--------------|
| `user_message` | 用户原始文本 | 是 | 是 |
| `assistant_response` | 仅对话结论 | 是 | 是 |
| `tool_calls` | 结构化工具使用记录 | 否（仅 meta） | 是 |

### 服务端行为

- **Events 管线**（`_write_immediate` + `_merge_buffer`）：仅使用 `user_message` + `assistant_response` 作为正文和 embedding 输入
- **Observer**（`record_batch`）：接收完整轮次数据，包含 `tool_calls`，保证转录完整性
- **tool_calls**：存为 event 记录的 `meta.tool_calls`——可查询的结构化元数据，不进入正文，不参与 embedding

### 向后兼容

- `tool_calls` 是可选字段——不传时行为与当前完全一致
- 当 `tool_calls` 存在时，服务端将 `assistant_response` 视为精炼摘要，`tool_calls` 仅存入 metadata
- 无需存储 schema 迁移——`meta` 字段已支持任意 JSON

## 改动明细

### MCP 插件（`plugins/opencortex-memory/`）

**`lib/mcp-server.mjs`** — 更新工具定义和处理函数：

```javascript
// 工具定义：新增 tool_calls 参数
add_message: [null, null,
  '...现有描述...',
  {
    user_message:       { type: 'string', description: "用户消息", required: true },
    assistant_response: { type: 'string', description: '你的对话结论——发现了什么、做了什么决策、有什么建议。不要包含工具输出、代码块或执行细节。', required: true },
    tool_calls:         { type: 'array',  description: '本轮使用的工具列表。每项包含 {name, summary}，summary 为工具操作的一句话描述。' },
    cited_uris:         { type: 'array',  description: '回复中引用的 opencortex:// URI' },
  }],

// handleAddMessage：透传 tool_calls
async function handleAddMessage(args) {
  const turnId = _lastRecallTurnId || `t${++_turnCounter}`;
  const body = {
    session_id: _sessionId,
    phase: 'commit',
    turn_id: turnId,
    messages: [
      { role: 'user', content: args.user_message },
      { role: 'assistant', content: args.assistant_response },
    ],
  };
  if (args.tool_calls) body.tool_calls = args.tool_calls;
  if (args.cited_uris) body.cited_uris = args.cited_uris;
  return await httpContextCall(body);
}
```

### HTTP 模型（`src/opencortex/http/models.py`）

**`ContextRequest`** — 新增 `tool_calls` 字段：

```python
class ToolCallRecord(BaseModel):
    name: str
    summary: str = ""

class ContextRequest(BaseModel):
    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turn_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    phase: str
    messages: Optional[List[ContextMessage]] = None
    tool_calls: Optional[List[ToolCallRecord]] = None  # 新增
    cited_uris: Optional[List[str]] = None
    config: Optional[ContextConfig] = None
```

### HTTP 路由（`src/opencortex/http/server.py`）

**`context_handler`** — 当前未转发 `tool_calls`，需要补上：

```python
# 当前（L455-469）：
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
        # ⚠️ 缺少 tool_calls 转发
    )

# 修改后：新增 tool_calls 参数透传
    return await _orchestrator._context_manager.handle(
        ...
        tool_calls=[t.model_dump() for t in req.tool_calls] if req.tool_calls else None,
    )
```

### Context Manager（`src/opencortex/context/manager.py`）

**`handle()`** — 接收并转发 `tool_calls` 到 commit：

```python
async def handle(self, ..., tool_calls=None):
    ...
    elif phase == "commit":
        return await self._commit(
            session_id, turn_id, messages, tenant_id, user_id,
            cited_uris, tool_calls,
        )
```

**`_commit()`** — tool_calls 存为元数据 + immediate 写入并行化：

```python
async def _commit(self, ..., tool_calls=None):
    # Observer 接收完整消息 + tool_calls（扩展 record_batch）
    self._observer.record_batch(
        session_id, messages, tenant_id, user_id,
        tool_calls=tool_calls,
    )

    # 预构建写入任务列表（不改变 buffer 状态）
    buffer = self._conversation_buffers.setdefault(sk, ConversationBuffer())
    write_items = []
    for i, msg in enumerate(messages):
        text = msg.get("content", "")
        if not text:
            continue
        role = msg.get("role", "")
        idx = buffer.start_msg_index + len(buffer.messages) + i
        tc = tool_calls if role == "assistant" else None
        write_items.append((text, idx, tc))

    # 并行执行所有 embed + upsert（核心性能优化）
    tokens_for_identity = set_request_identity(tenant_id, user_id)
    try:
        results = await asyncio.gather(*[
            self._orchestrator._write_immediate(
                session_id=session_id, msg_index=idx,
                text=text, tool_calls=tc,
            )
            for text, idx, tc in write_items
        ], return_exceptions=True)
    finally:
        reset_request_identity(tokens_for_identity)

    # 按序更新 buffer（gather 保证结果顺序与输入一致）
    for (text, idx, tc), result in zip(write_items, results):
        if isinstance(result, Exception):
            logger.warning("[ContextManager] Immediate write failed: %s", result)
            continue
        buffer.messages.append(text)
        buffer.immediate_uris.append(result)
        buffer.token_count += self._estimate_tokens(text)

    # 合并阈值检查（不变，fire-and-forget）
    if buffer.token_count >= 1000:
        task = asyncio.create_task(self._merge_buffer(...))
        ...
```

### Orchestrator（`src/opencortex/orchestrator.py`）

**`_write_immediate()`** — 接收并存储 `tool_calls` 为元数据：

```python
async def _write_immediate(self, session_id, msg_index, text, tool_calls=None):
    ...
    record = {
        ...
        "meta": {
            "layer": "immediate",
            "msg_index": msg_index,
            "session_id": session_id,
            "tool_calls": tool_calls or [],  # 结构化元数据，不进入正文/embedding
        },
    }
```

### Observer（`src/opencortex/alpha/observer.py`）

**`record_batch()`** — 当前只保存 role/content/timestamp，需要扩展以接收 tool_calls：

```python
# 当前：
def record_batch(self, session_id, messages, tenant_id, user_id):
    for msg in messages:
        self._transcripts[session_id].append({
            "role": msg["role"],
            "content": msg["content"],
            "timestamp": msg.get("timestamp", time.time()),
        })

# 修改后：新增 tool_calls 参数，追加到最后一条 assistant 消息中
def record_batch(self, session_id, messages, tenant_id, user_id, tool_calls=None):
    for msg in messages:
        entry = {
            "role": msg["role"],
            "content": msg["content"],
            "timestamp": msg.get("timestamp", time.time()),
        }
        # 将 tool_calls 附加到 assistant 消息，保证转录完整性
        if msg["role"] == "assistant" and tool_calls:
            entry["tool_calls"] = tool_calls
        self._transcripts[session_id].append(entry)
```

### _merge_buffer tool_calls 聚合

**问题**：immediate 记录在 merge 后被删除，如果 tool_calls 只存在 immediate 的 meta 上，merged event 会丢失这些信息。

**方案**：`_merge_buffer()` 合并时，从 buffer 收集所有 tool_calls 并聚合到 merged event 的 meta 中：

```python
async def _merge_buffer(self, sk, session_id, tenant_id, user_id):
    buffer = self._conversation_buffers.get(sk)
    ...
    # 聚合 buffer 中所有 tool_calls
    all_tool_calls = []
    for tc_list in buffer.tool_calls_per_turn:
        all_tool_calls.extend(tc_list)

    await self._orchestrator.add(
        abstract="",
        content=combined,
        category="events",
        context_type="memory",
        meta={
            "layer": "merged",
            "ingest_mode": "memory",
            "msg_range": [...],
            "session_id": session_id,
            "tool_calls": all_tool_calls,  # 聚合后持久化到 merged event
        },
        session_id=session_id,
    )
```

同时扩展 `ConversationBuffer`：

```python
@dataclass
class ConversationBuffer:
    messages: list = dc_field(default_factory=list)
    token_count: int = 0
    start_msg_index: int = 0
    immediate_uris: list = dc_field(default_factory=list)
    tool_calls_per_turn: list = dc_field(default_factory=list)  # 新增
```

### MCP Prompt（`usage-guide`）

更新 `add_message` 使用指引，明确三路分离规则：

```
### Step 3: add_message（回复后调用）
- user_message：用户的原始消息
- assistant_response：仅填写你的对话结论——发现了什么、做了什么决策、有什么建议
  - 不要包含：工具输出、代码块、命令结果、diff、日志
  - 应该包含：决策、发现、下一步计划、解释说明
- tool_calls：本轮使用的工具列表，每项包含 {name, summary}
```

## 改动文件汇总

| 文件 | 改动内容 |
|------|---------|
| `plugins/opencortex-memory/lib/mcp-server.mjs` | `add_message` 工具定义 + `handleAddMessage` 处理函数 |
| `src/opencortex/http/models.py` | `ToolCallRecord` 模型 + `ContextRequest.tool_calls` 字段 |
| `src/opencortex/http/server.py` | `context_handler` 转发 `req.tool_calls` 到 `handle()` |
| `src/opencortex/context/manager.py` | `handle()` 接收 `tool_calls`；`_commit()` 透传 + buffer 收集；`ConversationBuffer` 新增 `tool_calls_per_turn`；`_merge_buffer()` 聚合 tool_calls 到 merged meta |
| `src/opencortex/orchestrator.py` | `_write_immediate()` 将 `tool_calls` 存入 `meta` |
| `src/opencortex/alpha/observer.py` | `record_batch()` 接收 `tool_calls` 参数，附加到 assistant 转录条目 |

不需要改动：存储 schema、collection 字段、前端。

## 性能优化

### 问题

当前 `_commit()` 对每条消息**串行**执行 `_write_immediate()`，每次调用包含：

1. `embed()` — CPU 密集的模型推理（`run_in_executor`，2s timeout）
2. `storage.upsert()` — Qdrant 网络 I/O

一个典型 commit 含 2 条消息（user + assistant），串行时总耗时 ≈ 2× 单次 embed 时间。

### 优化

用 `asyncio.gather()` 并行执行所有 `_write_immediate()` 调用：

```
优化前（串行）：
  embed(user) → upsert(user) → embed(assistant) → upsert(assistant)
  总耗时 ≈ T_embed × 2

优化后（并行）：
  embed(user)      → upsert(user)      ─┐
  embed(assistant)  → upsert(assistant)  ─┘ gather → 更新 buffer
  总耗时 ≈ T_embed × 1
```

### 注意事项

- `asyncio.gather()` 保证结果顺序与输入一致，buffer 更新无需额外排序
- `set_request_identity()` 使用 contextvars，在 gather 前设置一次即可，所有子协程继承同一 context
- `return_exceptions=True` 确保单条消息失败不阻塞其他消息写入
- `_merge_buffer` 已经是 fire-and-forget（`asyncio.create_task`），不受影响

## 验证方式

部署后：

- 在 Memory Console 检查最近的 `events`——正文应仅包含对话结论
- 检查 event 记录的 `meta.tool_calls`——工具使用应以结构化元数据形式存在
- 用对话类查询测试 recall 质量——结果应更干净
- 测试向后兼容——不传 `tool_calls` 的 `add_message` 调用行为应与当前一致

## 风险

- LLM 可能在 `assistant_response` 中仍然包含执行细节，尽管工具描述已更新
  - 缓解：prompt 更新 + 字段分离已覆盖大部分场景；残余噪声边际且可接受
  - 后续可选：当 `tool_calls` 存在且 `assistant_response` 超过 1000 字符时，进行轻量长度检查（可能仍含噪声）
- 不发送 `tool_calls` 的现有 MCP 客户端保持当前行为
  - 缓解：向后兼容设计——`tool_calls` 为可选字段
