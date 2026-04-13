# Knowledge 生命周期补全方案

**日期**: 2026-04-03
**状态**: 待实施
**来源**: Codex 代码审计 + 人工验证 + Codex 对抗审查修正

---

## 背景

Cortex Alpha 知识管线（Observer → TraceSplitter → Archivist → Sandbox → KnowledgeStore）代码已实现，但集成链路存在 7 处断点，导致知识从未真正完成 CANDIDATE → VERIFIED → ACTIVE 的生命周期流转。当前状态是"代码存在但链路不通"。

**额外发现（对抗审查）**：knowledge_store.py 中的过滤 DSL 与 filter_translator.py 不匹配。现有代码使用 `{"field": ..., "op": "=", "value": ...}` 格式，但 translator 只识别 `{"op": "must", "field": ..., "conds": [...]}` 格式。这意味着 knowledge_store.py 的所有过滤条件当前**全部失效**（退化为空过滤，匹配所有记录）。因管线默认关闭，此 bug 一直未暴露。

## 问题清单

| # | 严重度 | 文件:行号 | 问题 |
|---|--------|----------|------|
| 0 | CRITICAL | `knowledge_store.py:81-91` | **全部过滤 DSL 格式错误**，`conditions`/`op:"="` 不被 filter_translator 识别，过滤完全失效 |
| 1 | HIGH | `orchestrator.py:2439` | Sandbox.evaluate() 未接入 _run_archivist()，知识候选直接保存为 CANDIDATE，跳过验证 |
| 2 | HIGH | `orchestrator.py:2446` | source_trace_ids 未收集，Sandbox 无法获取证据集进行统计门控和 LLM 验证 |
| 3 | HIGH | `knowledge_store.py:38` | 保存知识前未根据 Sandbox 结果设置最终状态，所有知识永远停留在 CANDIDATE |
| 4 | MEDIUM | `orchestrator.py:2433` | session_end() 返回值缺少 knowledge_candidates 字段，context/manager.py:560 读到的永远是 0 |
| 5 | MEDIUM | `context/manager.py:176` | include_knowledge 默认 False，即使产出 ACTIVE 知识也无法进入 recall 主召回 |
| 6 | CRITICAL | `knowledge_store.py:73` | search() 签名含 user_id 但未用于过滤，user-scope 知识可被同租户其他用户读取 |
| 7 | MEDIUM | `config.py:44,47` | trace_splitter_enabled 和 archivist_enabled 默认 False，管线默认关闭 |

## 修复方案

### 修复 0（新增）：修正 knowledge_store.py 全部过滤 DSL

**文件**: `src/opencortex/alpha/knowledge_store.py`

**问题**: 全文件使用了错误的过滤 DSL 格式。正确的 DSL 参考 `context/manager.py:540-544`：

```python
# 正确格式（context/manager.py 实际使用的）
{"op": "and", "conds": [
    {"op": "must", "field": "session_id", "conds": [session_id]},
    {"op": "must", "field": "meta.layer", "conds": ["immediate"]},
]}

# 错误格式（knowledge_store.py 当前使用的）
{"op": "and", "conditions": [
    {"field": "tenant_id", "op": "=", "value": tenant_id},
]}
```

**修复 search()** (行 73-94):
```python
async def search(
    self, query: str, tenant_id: str, user_id: str,
    types: Optional[List[str]] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Vector search over knowledge — only active items returned."""
    embed_result = self._embedder.embed_query(query)

    # 基础过滤：tenant + 状态
    must_conds = [
        {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        {"op": "must", "field": "status", "conds": [s.value for s in SEARCHABLE_STATUSES]},
    ]

    if types:
        must_conds.append({"op": "must", "field": "knowledge_type", "conds": types})

    # --- 修复 6：scope + user_id 过滤 ---
    # user-scope 知识只对所有者可见，tenant/global 对同租户所有用户可见
    scope_filter = {"op": "or", "conds": [
        {"op": "must", "field": "scope", "conds": [
            KnowledgeScope.TENANT.value,
            KnowledgeScope.GLOBAL.value,
        ]},
        {"op": "and", "conds": [
            {"op": "must", "field": "scope", "conds": [KnowledgeScope.USER.value]},
            {"op": "must", "field": "user_id", "conds": [user_id]},
        ]},
    ]}
    must_conds.append(scope_filter)

    filter_expr = {"op": "and", "conds": must_conds}
    return await self._storage.search(
        self._collection, embed_result.dense_vector, filter_expr, limit=limit
    )
```

**修复 list_candidates()** (行 133-145):
```python
async def list_candidates(self, tenant_id: str) -> List[Dict[str, Any]]:
    """List knowledge items pending approval."""
    filter_expr = {"op": "and", "conds": [
        {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        {"op": "must", "field": "status", "conds": [
            KnowledgeStatus.CANDIDATE.value,
            KnowledgeStatus.VERIFIED.value,
        ]},
    ]}
    return await self._storage.filter(self._collection, filter_expr)
```

**改动范围**: search() 和 list_candidates() 完全重写过滤逻辑，约 30 行。修复 6（user_id 过滤）已在此一并完成。

**过滤语义**:
- `scope=USER` → 仅 `user_id` 匹配的记录可见
- `scope=TENANT` → 同 `tenant_id` 下所有用户可见
- `scope=GLOBAL` → 同 `tenant_id` 下所有用户可见（跨租户需要单独处理）

---

### 修复 1+2+3：接入 Sandbox 验证 + 收集证据 + 设置状态

**文件**: `src/opencortex/orchestrator.py` — `_run_archivist()` 方法

**Codex 对抗审查发现的关键问题**:
1. `sandbox` 是模块级函数 (`sandbox.evaluate()`)，不是对象方法。Orchestrator 当前没有 `self._sandbox` 属性。
2. `list_unprocessed()` 返回 `List[Dict[str, Any]]`，不是 `Trace` 对象，不能调用 `.trace_id` 或 `.to_dict()`。
3. 原文档丢掉了 `mark_processed()` 调用，会导致重复处理。

**当前代码** (行 2439-2468):
```python
async def _run_archivist(self, tenant_id: str, user_id: str) -> None:
    if not self._archivist or not self._trace_store or not self._knowledge_store:
        return
    try:
        traces = await self._trace_store.list_unprocessed(tenant_id)
        if not traces:
            return
        knowledge_items = await self._archivist.run(traces, tenant_id, user_id, KnowledgeScope.USER)
        for k in knowledge_items:
            await self._knowledge_store.save(k)
        # Mark traces as processed
        trace_ids = [t.get("trace_id", t.get("id", "")) for t in traces]
        trace_ids = [tid for tid in trace_ids if tid]
        if trace_ids:
            await self._trace_store.mark_processed(trace_ids)
    except Exception as exc:
        logger.warning("[Alpha] Archivist failed: %s", exc)
```

**修复后**:
```python
async def _run_archivist(self, tenant_id: str, user_id: str) -> Dict[str, int]:
    """Run Archivist in background to extract knowledge from traces."""
    stats = {"knowledge_candidates": 0, "knowledge_active": 0}
    if not self._archivist or not self._trace_store or not self._knowledge_store:
        return stats
    try:
        from opencortex.alpha.types import KnowledgeScope, KnowledgeStatus
        from opencortex.alpha.sandbox import evaluate as sandbox_evaluate

        traces = await self._trace_store.list_unprocessed(tenant_id)
        if not traces:
            return stats

        knowledge_items = await self._archivist.run(
            traces, tenant_id, user_id, KnowledgeScope.USER,
        )

        # Sandbox 配置（从 CortexAlphaConfig 读取）
        alpha_cfg = self._config.cortex_alpha

        for k in knowledge_items:
            # --- 修复 2：收集证据 traces（traces 是 List[Dict]） ---
            source_ids = set(k.source_trace_ids) if k.source_trace_ids else set()
            evidence_traces = [
                t for t in traces
                if t.get("trace_id", t.get("id", "")) in source_ids
            ]

            # --- 修复 1：调用 Sandbox 模块级函数 ---
            if evidence_traces and self._llm_completion:
                eval_result = await sandbox_evaluate(
                    knowledge_dict=k.to_dict(),
                    traces=evidence_traces,  # 已经是 List[Dict]
                    llm_fn=self._llm_completion,
                    min_traces=alpha_cfg.sandbox_min_traces,
                    min_success_rate=alpha_cfg.sandbox_min_success_rate,
                    min_source_users=alpha_cfg.sandbox_min_source_users,
                    min_source_users_private=alpha_cfg.sandbox_min_source_users_private,
                    llm_sample_size=alpha_cfg.sandbox_llm_sample_size,
                    llm_min_pass_rate=alpha_cfg.sandbox_llm_min_pass_rate,
                    require_human_approval=alpha_cfg.sandbox_require_human_approval,
                    user_auto_approve_confidence=alpha_cfg.user_auto_approve_confidence,
                )
                # --- 修复 3：根据结果设置状态 ---
                status_map = {
                    "needs_more_traces": KnowledgeStatus.CANDIDATE,
                    "needs_improvement": KnowledgeStatus.CANDIDATE,
                    "verified": KnowledgeStatus.VERIFIED,
                    "active": KnowledgeStatus.ACTIVE,
                }
                k.status = status_map.get(eval_result.status, KnowledgeStatus.CANDIDATE)

            await self._knowledge_store.save(k)

            if k.status == KnowledgeStatus.ACTIVE:
                stats["knowledge_active"] += 1
            else:
                stats["knowledge_candidates"] += 1

        # --- 保留 mark_processed（幂等性保障）---
        trace_ids = [t.get("trace_id", t.get("id", "")) for t in traces]
        trace_ids = [tid for tid in trace_ids if tid]
        if trace_ids:
            await self._trace_store.mark_processed(trace_ids)

        logger.info(
            "[Alpha] Archivist: %d candidates, %d active from %d traces",
            stats["knowledge_candidates"], stats["knowledge_active"], len(traces),
        )
    except Exception as exc:
        logger.warning("[Alpha] Archivist failed: %s", exc)
    return stats
```

**关键修正点**:
- `sandbox.evaluate()` 作为模块级函数直接 import 调用，无需 `self._sandbox`
- `traces` 是 `List[Dict]`，用 `t.get("trace_id")` 而非 `t.trace_id`
- `evidence_traces` 已经是 `List[Dict]`，直接传给 sandbox，无需 `.to_dict()`
- sandbox 配置参数从 `self._config.cortex_alpha` 读取（已在 CortexAlphaConfig 中定义）
- **保留了 `mark_processed()`**，确保不会重复处理

---

### 修复 4：session_end() 返回知识统计

**文件**: `src/opencortex/orchestrator.py` — `session_end()` 方法

**当前代码** (行 2425-2437):
```python
# Check Archivist trigger
if self._archivist and self._trace_store:
    count = await self._trace_store.count_new_traces(tid)
    if self._archivist.should_trigger(count):
        asyncio.create_task(self._run_archivist(tid, uid))
# ...
return {
    "session_id": session_id,
    "quality_score": quality_score,
    "alpha_traces": alpha_traces_count,
}
```

**修复后**:
```python
# Check Archivist trigger
archivist_stats = {"knowledge_candidates": 0, "knowledge_active": 0}
if self._archivist and self._trace_store:
    count = await self._trace_store.count_new_traces(tid)
    if self._archivist.should_trigger(count):
        # 改为 await 而非 create_task，以获取统计结果
        archivist_stats = await self._run_archivist(tid, uid)
# ...
return {
    "session_id": session_id,
    "quality_score": quality_score,
    "alpha_traces": alpha_traces_count,
    **archivist_stats,
}
```

**注意**: `_run_archivist` 从 `create_task`（fire-and-forget）改为 `await`（同步等待）。这会增加 session_end 延迟，但保证统计值可返回。如果延迟不可接受，可保持 `create_task` 但放弃实时统计，`knowledge_candidates` 报 0，通过后续查询获取真实值。

**改动范围**: session_end() 约 8 行。

---

### 修复 5：knowledge recall 可控开启

**文件**: `src/opencortex/config.py` + `src/opencortex/context/manager.py`

**Codex 审查发现**: ContextManager 没有 `self._alpha_config` 属性。需通过 orchestrator 间接访问。

**config.py** — CortexAlphaConfig 新增字段（行 64 后）:
```python
knowledge_recall_enabled: bool = False  # 服务端控制知识召回默认值
```

**context/manager.py** (行 176) — 修改 include_knowledge 默认值逻辑:
```python
# 优先级：客户端显式传入 > 服务端配置 > 默认 False
_server_default = False
if hasattr(self._orchestrator, '_config') and self._orchestrator._config:
    _server_default = self._orchestrator._config.cortex_alpha.knowledge_recall_enabled
include_knowledge = config.get("include_knowledge", _server_default)
```

**改动范围**: config.py 加 1 行字段，manager.py 改约 4 行。

---

### 修复 6：knowledge search 补全 user_id + scope 过滤

**已在修复 0 中一并完成。** search() 的 scope 过滤使用正确的 DSL 格式。

---

### 修复 7：提供可运行配置 profile

**文件**: `src/opencortex/config.py` — 不改默认值（保持向后兼容）

配置键名是 `cortex_alpha`（不是 `alpha`），与 `CortexConfig` 字段名一致。

**server.json 正确启用 profile**:
```json
{
  "cortex_alpha": {
    "trace_splitter_enabled": true,
    "archivist_enabled": true,
    "knowledge_recall_enabled": true,
    "archivist_trigger_threshold": 20
  }
}
```

**环境变量覆盖**（已在 `_apply_env_overrides` 中支持）:
```bash
OPENCORTEX_CORTEX_ALPHA='{"trace_splitter_enabled":true,"archivist_enabled":true,"knowledge_recall_enabled":true}'
```

**改动范围**: 仅补充文档/注释。config 加载逻辑已正确处理 `cortex_alpha` 嵌套字段（见 `_load_from_file` 行 212-213）。

---

## 实施顺序

```
修复 0 (CRITICAL, DSL修正) ─┐
                             ├── 第一批：过滤 DSL 修正（含修复 6）
修复 0 包含修复 6           ─┘

修复 1+2+3 (核心链路) ──→ 修复 4 (返回值) ──→ 修复 5 (recall 开启)
         第二批                    第三批              第三批

修复 7 (配置文档) ── 任意时间
```

- **第一批**: 修复 0（DSL 修正 + user_id 过滤，安全问题）
- **第二批**: 修复 1+2+3（Sandbox 接入，`_run_archivist` 一个方法内完成）
- **第三批**: 修复 4（返回值）+ 修复 5（recall 开启）+ 修复 7（配置文档）

---

## 验收标准

| # | 验收项 | 验证方式 |
|---|--------|----------|
| 1 | knowledge_store 过滤条件正确生效 | 单元测试：构造 filter_expr，验证 translate_filter 产出非空 Qdrant Filter |
| 2 | user-scope knowledge 不被同租户其他用户召回 | 测试：用户 A 的 user-scope 知识，用户 B 搜索不到 |
| 3 | session_end 后产生 trace | 启用 alpha 配置，执行一次完整会话，检查 traces collection |
| 4 | 达到阈值后 Archivist 生成 knowledge candidate | 积累 ≥ 20 条 trace 后触发，检查 knowledge collection |
| 5 | Sandbox 将部分 candidate 变为 VERIFIED/ACTIVE | 检查 knowledge collection 中存在非 CANDIDATE 状态记录 |
| 6 | knowledge_search 能搜到 ACTIVE 项 | 调用 search API，返回 ACTIVE 知识 |
| 7 | prepare() 开启 knowledge recall 时返回 knowledge | 配置 knowledge_recall_enabled=true，验证 recall 响应含 knowledge |
| 8 | 重复调用 _run_archivist 不会产生重复知识 | 验证 mark_processed 正确执行，二次调用返回空 |

---

## 影响范围

| 文件 | 改动类型 | 行数估算 |
|------|----------|----------|
| `src/opencortex/alpha/knowledge_store.py` | 修复全部过滤 DSL + user_id/scope 过滤 | ~30 行 |
| `src/opencortex/orchestrator.py` | 修改 `_run_archivist()` + `session_end()` | ~40 行 |
| `src/opencortex/context/manager.py` | 修改 `include_knowledge` 默认值逻辑 | ~4 行 |
| `src/opencortex/config.py` | 新增 `knowledge_recall_enabled` 字段 | ~1 行 |
| 测试 | 新增过滤 DSL 验证 + scope 隔离测试 | ~50 行 |
| **总计** | | **~125 行** |

不新增文件，不修改现有数据模型，不修改记忆体系代码。
