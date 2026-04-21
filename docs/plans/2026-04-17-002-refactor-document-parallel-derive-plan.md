---
title: "refactor: Document + Conversation OpenViking store alignment"
type: refactor
status: active
date: 2026-04-17
origin: docs/brainstorms/2026-04-17-document-ingestion-parallel-derive-requirements.md
related: docs/plans/2026-04-16-004-refactor-conversation-semantic-merge-plan.md
deepened: 2026-04-17
---

# refactor: Document + Conversation OpenViking store alignment

## Overview

本计划统一对齐 OpenViking 的 store 策略，覆盖 document mode 和 conversation mode 两条路径。

**Document 部分：** 串行 `_derive_layers()` 导致中等文档超时（QASPER 281 篇 0 成功）。改为并发 + bottom-up 父节点汇总，提升 client timeout。对标 OpenViking §4.6（document/resource bottom-up L0/L1）。

**Conversation 部分：** 当前 session 结束后 merged records 直接保留在搜索面上，没有 session-level summary 对象，也没有 final supersede 机制。同时 immediate 删除时遗留 anchor projection 孤儿记录。对标 OpenViking §4.3-§4.4（live session → archived session：session summary + 降级旧层级）。

conversation semantic segmentation 由已有 plan-004 覆盖（`docs/plans/2026-04-16-004-refactor-conversation-semantic-merge-plan.md`），本计划 Unit 6-9 是其前置或后续步骤，两个计划共同完成 conversation 对齐。

## Problem Frame

### Document

根因链：`POST /store` → `orchestrator.add()` → `_add_document()` → `for chunk in chunks: self.add()` → `_derive_layers()`（串行，每次 5-30s）。QASPER 中位数 10 chunks × 10s = 100s，远超 MCP client 30s timeout。

附带缺陷：`_add_document` 创建 parent record 时 `is_leaf=False` 导致 `_derive_layers` 被跳过（`orchestrator.py:1937`），父节点缺少 LLM 生成的 L0/L1。这阻塞了 object-first 检索策略在 document mode 的生效。

(see origin: `docs/brainstorms/2026-04-17-document-ingestion-parallel-derive-requirements.md`)

### Conversation

三个结构性缺口 vs OpenViking §4.3-§4.4：

1. **无 session summary**：`session_end()` 只做 trace split（`orchestrator.py:4528`），`_end()` 在 merge flush 后调用 `_persist_conversation_source`（原文归档）和 `_spawn_full_recompose_task`（merged leaf 收敛），但不生成 session-level summary 对象。OpenViking 要求 archived session 有 L1 structured summary → L0 compressed abstract。
2. **无 final supersede**：merged records 在 session 结束后保留全量搜索面可见性。OpenViking 要求 session summary 创建后，merged records 降级或退出搜索面（supersede 关系），避免陈旧 session 碎片在长期使用中积累噪声。
3. ~~**Anchor projection 孤儿**~~ — **经验证不存在**：`remove_by_uri` 已实现级联删除（精确匹配 + `{uri}/` 前缀），anchor/fp 投影记录随 immediate 一起删除。

(see design: `docs/design/2026-04-14-opencortex-openviking-borrowable-retrieval-optimization.md` §4.3, §4.4, §8.4)

## Requirements Trace

### Document (R1-R11)

- R1. `_add_document` 叶子 chunk 处理从串行改为并发（semaphore 限流）
- R2. 并发度通过 `CortexConfig` 可配置，默认 3
- R3. `parent_index` 依赖关系正确：扁平文档直接 gather，嵌套文档按拓扑层级
- R4. 复用 `batch_add` 的 semaphore + gather 模式
- R5. 叶子 derive 完成后，父节点从孩子 L0 abstract 汇总生成 L1 overview
- R6. 父节点 L0 abstract 从 L1 overview 压缩导出
- R7. 多层嵌套文档自底向上逐层执行
- R8. 父节点汇总 1-2 次额外 LLM 调用
- R9. MCP client store timeout 从 30s → 300s
- R10. Python HTTP client store timeout 同步提升
- R11. Recall p50 延迟 < 1s（硬性性能约束：本次改造不能劣化检索性能）

### Cross-cutting (R12-R13)

- R12. `_derive_layers` prompt 对齐 OpenViking §4.5 三层同源：LLM 同时生成 L0 abstract + L1 overview（当前 L0 是纯代码截取 overview 首句，质量不足）。`_derive_abstract_from_overview` 降级为 no-LLM fallback
- R13. Conversation merge token 阈值从 1000 → 2000（当前 1000 ≈ 500-700 汉字，窗口太小导致语义分段无法产出有意义的多段）

### Conversation (R14-R20)

- ~~R14. Anchor projection 孤儿~~ — **经验证不存在**：`remove_by_uri` 已实现级联删除（精确匹配 + `{uri}/` 前缀），`_delete_immediate_families` 调用它时已经连带删除 `/anchors/...` 和 `/fact_points/...` 投影记录
- R15. Conversation semantic segmentation 由 plan-004 覆盖，本计划不重复
- R16. Session 结束时从 merged records 的 abstract 底层汇总生成 session-level summary 对象（L1 overview → L0 abstract），对标 OpenViking §4.4
- R17. Session summary 对象写入后，该 session 的 merged records 退出主搜索面（final supersede），避免长期噪声积累
- R18. Final supersede 使用 soft delete（标记 `meta.superseded=True` + 过滤条件），不物理删除 merged records，保留可审计性
- R19. Session summary 是 `is_leaf=True` 的持久记录，进入正常搜索面
- R20. Session summary 生成失败时 merged records 保持原状（graceful degradation）

## Scope Boundaries

### Document
- 不改 MarkdownParser chunking 逻辑
- 不引入异步 job 系统
- 不做微小 chunk 合并
- 不修复 `embed_text` 使用空 `chunk_abstract` 的 pre-existing 问题（独立 issue）
- QASPER adapter `expected_uris` 膨胀问题不在此范围

### Conversation
- Semantic segmentation 由 plan-004 覆盖，本计划仅处理 session summary（Unit 7）和 final supersede（Unit 8）
- 不改 conversation 的 immediate write 逻辑（merge buffer 阈值调整在 R13 范围内）
- 不引入 multi-level conversation tree 或 graph propagation
- 不改 `_build_recomposition_segments` 分段算法（plan-004 范围）
- 不改 recall/search 的主检索路径（仅增加 `superseded` 过滤条件）

## Context & Research

### Relevant Code and Patterns

#### Document

- **串行循环**：`orchestrator.py:1176` — `for idx, chunk in enumerate(chunks):` 每个 chunk await `self.add()`
- **`is_leaf` 门控**：`orchestrator.py:1937` — `if content and is_leaf:` 跳过 `is_leaf=False` 的 derive
- **`batch_add` 并发模式**：`orchestrator.py:4822` — `asyncio.Semaphore(_BATCH_ADD_CONCURRENCY)` + `asyncio.gather(*tasks, return_exceptions=True)` — 本计划的参考实现
- **`chunked_llm_derive` 并发模式**：`utils/text.py:157` — 类似 semaphore + gather 模式
- **`_derive_layers` 快速路径**：`orchestrator.py:1236` — `if user_abstract and user_overview: return {empty}`
- **`update()` 方法**：`orchestrator.py:2315` — 支持 abstract/content/meta 更新，但缺 `overview` 参数
- **`build_overview_compression_prompt`**：`prompts.py:308` — 压缩多段 overview 为单段，可参考
- **`build_layer_derivation_prompt`**：`prompts.py:169` — 叶子 derive 的 prompt 模板
- **`is_dir_chunk` 检测**：`orchestrator.py:1186` — `any(c.parent_index == idx for c in chunks[idx+1:])`
- **timeout 链**：`tools.ts:178`（30s MCP）、`http/client.py:26`（30s Python）、`lifecycle.ts:50`（30s context API）

#### Cross-cutting (L0/L1 + merge threshold)

- **`build_layer_derivation_prompt`**：`prompts.py:169` — 当前输出 JSON 只含 `overview`（L1），不含 `abstract`（L0）。L0 由 `_derive_abstract_from_overview`（`orchestrator.py:1444`）纯代码截取 overview 首句
- **`_derive_abstract_from_overview`**：`orchestrator.py:1444` — 截取 overview 第一句作为 L0。优先级：user_abstract > overview 首句 > content 截断。改为 LLM fallback 后此方法只在 no-LLM 场景使用
- **`_derive_layers` 调用链**：`orchestrator.py:1224` — 正常路径（`orchestrator.py:1310-1358`）和 chunked 路径（`orchestrator.py:1249-1303`）都解析 LLM JSON 输出。新增 `abstract` 字段后两条路径都需要读取
- **Merge 阈值**：`manager.py:1509` 和 `manager.py:1741` — 硬编码 `1000`，两处都需要改为 2000

#### Conversation

- **`_delete_immediate_families()`**：`manager.py:1781` — 逐 URI 调用 `remove_by_uri()`，该方法已实现级联删除（精确匹配 + `{uri}/` 前缀），anchor/fp 投影记录会连带删除
- **`_sync_anchor_projection_records()`**：`orchestrator.py:1736` — 为 `is_leaf=True` 记录创建 anchor + fact_point 投影，URI 使用 `_anchor_projection_prefix(source_uri)` 和 `_fact_point_prefix(source_uri)` 前缀
- **`_end()`**：`manager.py:1932` — session 结束流程：flush merge → cleanup immediates → `_persist_conversation_source` → `session_end()` → `_spawn_full_recompose_task`
- **`_spawn_full_recompose_task()`**：`manager.py:1576` — 异步 full-session merged-leaf 收敛（重新分段已有 merged records）。在 `_end` 的最后阶段触发，已有 task 管理（dedup + cleanup callback）
- **`_persist_conversation_source()`**：`manager.py:922` — 持久化 session 原始 transcript 为 `is_leaf=False` 的 source record
- **`_load_session_merged_records()`**：加载 session 的所有 `layer=merged` 记录，用于 full recomposition
- **`_merge_buffer()`**：`manager.py:1811` — 在线 merge 工作流，调用 `_build_recomposition_segments` 分段后写 merged leaves，然后清理 superseded merged 和 immediates
- **`remove_by_uri` 已支持级联删除**：`storage/qdrant/adapter.py:341` — 删除精确匹配 + 以 `{uri}/` 开头的所有子记录（MatchText candidate + literal startswith guard）
- **搜索过滤**：`hierarchical_retriever.py` 的 `_build_search_filter` 已包含 `is_leaf=True` 过滤。增加 `superseded` 过滤条件需在此处添加

### Institutional Learnings

- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`：timeout 和 degrade 决策属于 runtime 阶段，不应泄漏到 probe/planner。本计划的 timeout 调整遵循此原则（仅改 client 层）。

## Key Technical Decisions

### Document

- **Semaphore 作用域覆盖完整 `self.add()` 调用**：包含 LLM derive + embed + Qdrant upsert。匹配 `batch_add()` 模式，防止 retry 风暴和 Qdrant 写入竞争。
- **`chunk_results` 使用预分配列表**：`[None] * len(chunks)` 预分配，`gather` 返回后按索引写入。保证 `parent_index` 引用在并发下的正确性。
- **父节点更新通过扩展 `update()` 方法**：给 `update()` 添加 `overview` 参数，避免创建新的更新路径。当 abstract + overview 同时传入时，`_derive_layers` 命中快速路径跳过重复 LLM 调用。
- **Section-level 节点也做 bottom-up 汇总**：R7 要求多层嵌套逐层执行。Section 节点虽然 `is_leaf=False` 被过滤出搜索结果，但需要 L0/L1 以支持 Console 前端的层级浏览和未来 object-first 检索。
- **MCP timeout 按工具区分**：仅 store/batch_store 提升到 300s，其他工具保持 30s。避免 search/recall 在异常时等待过久。
- **返回的 `parent_ctx` 对象就地更新**：bottom-up 完成后更新 `parent_ctx.abstract` 和 `parent_ctx.overview`，避免 Qdrant re-read。

### Cross-cutting

- **Prompt 增加 `abstract` 输出字段，`_derive_abstract_from_overview` 降级为 fallback**：`build_layer_derivation_prompt` 的 JSON 输出增加 `"abstract"` 字段，LLM 同时生成 L0+L1。`_derive_layers` 优先使用 LLM 返回的 abstract；仅当 LLM 未返回 abstract 或返回空时才回退到 `_derive_abstract_from_overview` 截取首句。这保证了向后兼容——老版本 LLM 不返回 abstract 也不会 break。
- **Merge 阈值 1000→2000 直接硬编码修改**：不引入配置项（YAGNI），两处（触发检查 + snapshot 检查）同步修改。2000 tokens ≈ 10-15 轮中文对话，给语义分段（plan-004）更大的窗口。

### Conversation

- **Anchor projection 孤儿问题已由现有代码解决**：`remove_by_uri` 已实现级联删除（精确 + `{uri}/` 前缀），`_delete_immediate_families` 调用它时连带删除 `/anchors/...` 和 `/fact_points/...`。无需额外 Unit。
- **Session summary 复用 bottom-up 汇总模式**：与 document parent summarization 使用相同的 `build_parent_summarization_prompt`（Unit 3）。输入是 merged records 的 abstract 列表，输出是 session L1 overview → L0 abstract。统一 document 和 conversation 的汇总路径。
- **Final supersede 用 soft delete（`meta.superseded=True`）而非物理删除**：merged records 保留可审计性和可恢复性。搜索过滤在 `_build_search_filter` 中增加 `superseded != True` 条件。这比物理删除安全——如果 session summary 生成后发现质量问题，可以回滚。
- **Session summary 在 `_end()` 中 full recompose 之后执行**：full recompose 先收敛 merged records，然后 session summary 从收敛后的 merged records 汇总。避免 summary 基于中间态的碎片化 merged records。
- **Session summary 写入为 `is_leaf=True` + `layer=session_summary`**：进入正常搜索面，成为 session 的持久代表。merged records 的 supersede 标记不影响 session summary 的搜索可见性。

## Open Questions

### Resolved During Planning

- **Q: 嵌套文档拓扑序实现** → 从 `ParsedChunk.parent_index` 构建 level map：`parent_index=-1` 为 level 0，引用 level N chunk 的为 level N+1。MarkdownParser 保证 parent 总在 children 之前（heading 先于 content）。chunk 写入按 level 从低到高执行（同 level 并发），bottom-up 汇总按 level 从高到低执行。
- **Q: 父节点汇总 prompt** → 新增 `build_parent_summarization_prompt(doc_title, children_abstracts)` 函数。输入为子节点 L0 abstract 列表，输出 JSON（abstract + overview + keywords）。与 `build_layer_derivation_prompt` 共享输出格式但输入不同（从子节点摘要汇总，非原文 derive）。
- **Q: MCP timeout 粒度** → 在 `callProxyTool()` 中根据 tool name 判断：`store`/`batch_store` 用 300000ms，其余 30000ms。Python client 在 `store()` 方法中 override timeout。

### Deferred to Implementation

- **exact topological sort 实现细节**：level map 的精确数据结构（dict vs list of sets）取决于实际 chunk 分布
- **parent summarization prompt 的精确措辞**：需要实际测试 LLM 输出质量后微调
- **Session summary 的最低 merged records 阈值**：session 只有 1 条 merged record 时是否仍生成 summary（可能不必要）。倾向 >= 2 条时触发
- **`superseded` 过滤条件的精确 Qdrant filter DSL**：需验证 `meta.superseded` 作为 payload field 的 filter 语法

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
_add_document(content, ...) :
  chunks = parser.parse_content(content)
  if len(chunks) <= 1: return self.add(...)          # 现有单 chunk 快速路径

  # Phase 0: 创建 parent record (is_leaf=False, abstract=doc_title)
  parent_ctx = await self.add(is_leaf=False, ...)
  doc_parent_uri = parent_ctx.uri

  # Phase 1: 预计算
  is_dir = [any(c.parent_index == i for c in chunks[i+1:]) for i in range(len(chunks))]
  levels = compute_topological_levels(chunks)         # {0: [idx...], 1: [idx...], ...}
  chunk_results = [None] * len(chunks)                # 预分配
  sem = asyncio.Semaphore(config.document_derive_concurrency)

  # Phase 2: 按拓扑层级并发写入 (top-down)
  for level in sorted(levels.keys()):
    async def process(idx):
      async with sem:
        parent = doc_parent_uri
        if chunks[idx].parent_index >= 0:
          parent = chunk_results[chunks[idx].parent_index].uri
        ctx = await self.add(
          content=chunks[idx].content,
          parent_uri=parent,
          is_leaf=not is_dir[idx],
          ...
        )
        chunk_results[idx] = ctx
        return ctx

    results = await asyncio.gather(
      *[process(idx) for idx in levels[level]],
      return_exceptions=True
    )

  # Phase 3: Bottom-up 汇总 (deepest level → shallowest → doc parent)
  for level in sorted(levels.keys(), reverse=True):
    section_indices = [i for i in levels[level] if is_dir[i]]
    for si in section_indices:
      children_abstracts = [chunk_results[j].abstract
                           for j in range(len(chunks))
                           if chunks[j].parent_index == si and chunk_results[j]]
      summary = await _derive_parent_summary(doc_title, children_abstracts)
      await self.update(chunk_results[si].uri, abstract=..., overview=..., meta=...)
      chunk_results[si].abstract = summary["abstract"]
      chunk_results[si].overview = summary["overview"]

  # Phase 4: 汇总 doc parent
  top_children = [chunk_results[i] for i in range(len(chunks))
                  if chunks[i].parent_index == -1 and chunk_results[i]]
  summary = await _derive_parent_summary(doc_title, [c.abstract for c in top_children])
  await self.update(parent_ctx.uri, abstract=..., overview=..., meta=...)
  parent_ctx.abstract = summary["abstract"]
  parent_ctx.overview = summary["overview"]

  return parent_ctx
```

### Conversation Session End Flow (directional)

> *Directional guidance — not implementation specification.*

```
_end(session_id, tenant_id, user_id):
  # Phase 1: existing — flush remaining buffer
  await _merge_buffer(sk, ..., flush_all=True)

  # Phase 2: existing — cleanup pending immediates
  if pending_immediate_cleanup: await _delete_immediate_families(...)

  # Phase 3: existing — persist conversation source
  source_uri = await _persist_conversation_source(...)

  # Phase 4: existing — trigger trace split
  await orchestrator.session_end(...)

  # Phase 5: existing — full recompose (async, converges merged records)
  _spawn_full_recompose_task(...)
  await _wait_for_full_recompose(sk)  # NEW: wait for convergence

  # Phase 6: NEW — session summary generation (Unit 7)
  merged_records = await _load_session_merged_records(session_id, source_uri)
  if len(merged_records) >= 2:
    abstracts = [r.get("abstract", "") for r in merged_records]
    summary = await _derive_session_summary(session_id, abstracts)
    summary_uri = _session_summary_uri(tenant_id, user_id, session_id)
    await orchestrator.add(
      uri=summary_uri,
      abstract=summary["abstract"],
      content=summary["overview"],  # L1 as content for searchability
      is_leaf=True,
      meta={"layer": "session_summary", "session_id": session_id, ...}
    )

    # Phase 7: NEW — final supersede (Unit 8)
    for record in merged_records:
      await orchestrator.update(record["uri"], meta={"superseded": True})
```

## Implementation Units

- [ ] **Unit 1: Add `document_derive_concurrency` config field**

**Goal:** 让 document 并发度可配置。

**Requirements:** R2

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/config.py`

**Approach:**
- 在 `CortexConfig` dataclass 中 `context_flattening_enabled` 之后添加 `document_derive_concurrency: int = 3`
- 自动获得 `OPENCORTEX_DOCUMENT_DERIVE_CONCURRENCY` 环境变量 override（`_apply_env_overrides` 已有 int 类型处理逻辑）

**Patterns to follow:**
- 现有 `immediate_event_ttl_hours: int = 24` 字段的命名和位置风格

**Test expectation:** none — config 字段由已有的 `_apply_env_overrides` 机制覆盖，无需独立测试

**Verification:**
- `CortexConfig().document_derive_concurrency == 3`
- 环境变量 `OPENCORTEX_DOCUMENT_DERIVE_CONCURRENCY=5` 生效

---

- [ ] **Unit 2: Extend `update()` with `overview` parameter + fix three existing hazards**

**Goal:** 让 `update()` 能直接设置 overview，为 bottom-up 父节点更新提供接口。同时修复三个会导致 bottom-up 更新出错的既有问题。

**Requirements:** R5, R6, R8

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Test: `tests/test_document_mode.py`

**Approach:**

*2a. 添加 `overview` 参数*
- 给 `update()` 签名添加 `overview: Optional[str] = None`
- 修改 `next_overview` 赋值逻辑：`overview if overview is not None else record.get("overview", "")`
- 当 `abstract` 和 `overview` 同时传入时，`_derive_layers` 快速路径自动生效（`orchestrator.py:1236`）

*2b. 修复 entity sync 的多余 LLM 调用（review 发现）*
- `orchestrator.py:2482` 的 entity sync block 对 `is_leaf=False` 记录会触发额外 `_derive_layers` 调用（不命中快速路径，因为不传 `user_overview`）
- 修复：在 entity sync block 入口添加 `is_leaf` 守卫 — `if record.get("is_leaf") is False: skip`。`is_leaf=False` 的 parent/section 节点不应走 entity sync（与 `_sync_anchor_projection_records` 的守卫一致）
- 若不修复：每个 bottom-up update 多 1 次 LLM 调用，违反 R8（1-2 次额外调用）

*2c. 修复 CortexFS write 的 L2 内容清除（review 发现）*
- `orchestrator.py:2476` 用 `content or ""` — 当 `content=None`（bottom-up 只改 abstract/overview 不改 content）时写入空字符串，清除父节点的 L2 全文
- 修复：改为 `next_content`（line 2370 已从 record 加载）

*2d. 修复 CortexFS write 不传 overview（review 发现）*
- `orchestrator.py:2474` 的 `write_context` 调用不传 `overview` 参数，导致 CortexFS 的 `.overview.md` 不更新（Qdrant/CortexFS 分歧）
- 修复：在 `write_context` 调用中添加 `overview=next_overview`

**Patterns to follow:**
- `update()` 中 `abstract` 参数的处理方式（line 2366-2369）
- `_sync_anchor_projection_records` 的 `is_leaf` 守卫模式

**Test scenarios:**
- Happy path: `update(uri, abstract="new", overview="new overview")` 后，Qdrant record 的 abstract_json 包含新 overview，CortexFS `.overview.md` 也更新
- Edge case: `update(uri, overview="only overview")` 仅更新 overview，abstract 保持不变
- Error path: `update(uri, abstract="x", overview="y")` 对 `is_leaf=False` 记录不触发 entity sync LLM 调用（mock LLM 被调用 0 次）
- Edge case: `update(uri, abstract="x")` 不改 content 时，CortexFS L2 内容保持不变（不被清除）
- Integration: update abstract+overview 后 `_derive_layers` 不被调用（快速路径生效）

**Verification:**
- `update()` 接受 `overview` 参数
- 传入 abstract + overview 时不触发额外 LLM 调用
- `is_leaf=False` 记录的 entity sync 被跳过
- CortexFS L2 内容在 `content=None` 时不被清除

---

- [ ] **Unit 3: Add bottom-up parent summarization prompt**

**Goal:** 新增 prompt 模板，从子节点 L0 abstract 汇总生成父节点 L1 overview + L0 abstract。

**Requirements:** R5, R6

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/prompts.py`

**Approach:**
- 新增 `build_parent_summarization_prompt(doc_title: str, children_abstracts: List[str]) -> str`
- 输入：文档标题 + 子节点 abstract 列表（编号呈现）
- 输出格式要求：JSON `{"abstract": "...", "overview": "...", "keywords": [...]}`
- 与 `build_layer_derivation_prompt` 共享输出结构但输入语义不同：从汇总生成，非从原文 derive
- 放在 `build_overview_compression_prompt` 之后（prompts.py section 10 末尾）

**Patterns to follow:**
- `build_overview_compression_prompt` 的输入拼接方式
- `build_layer_derivation_prompt` 的 JSON 输出格式

**Test scenarios:**
- Happy path: prompt 包含 doc_title 和所有 children abstracts
- Edge case: 单个 child abstract → prompt 仍生成有效格式
- Edge case: children_abstracts 为空列表 → prompt 仍可用（降级场景）

**Verification:**
- 函数存在且可调用
- 返回的 prompt 字符串包含所有输入的 children abstracts

---

- [ ] **Unit 4: Refactor `_add_document()` for concurrent chunk processing + bottom-up summarization**

**Goal:** 将串行 chunk 处理改为按拓扑层级并发，完成后自底向上生成父节点 L0/L1。

**Requirements:** R1, R3, R4, R5, R6, R7, R8

**Dependencies:** Unit 1, Unit 2, Unit 3

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Test: `tests/test_document_mode.py`

**Approach:**

*Phase 1: 预计算（替换 orchestrator.py:1174-1220 的串行循环）*
- 预计算 `is_dir_chunk` 列表（从 `chunks` 的 `parent_index` 推导，与当前 line 1186 逻辑相同但提前到循环外）
- 构建 topological level map：`parent_index=-1` 为 level 0，引用 level N chunk 的为 level N+1
- 预分配 `chunk_results = [None] * len(chunks)`
- 从 `self._config.document_derive_concurrency` 创建 `asyncio.Semaphore`

*Phase 2: 按拓扑层级并发写入*
- 对每个 level（从低到高），`asyncio.gather` 并发处理该 level 的所有 chunk
- 每个 chunk 的处理逻辑与当前相同：解析 `parent_uri`、判断 `chunk_role`、构建 `embed_text`、调用 `self.add()`
- **Section 节点（`is_dir[idx]=True`）使用 heading text 作为初始 abstract**（从 `chunk.meta.get("section_path", "").split(" > ")[-1]` 提取），而非当前的空字符串。这样即使 Phase 3 失败，section 节点也有比空 abstract 更好的降级状态
- `gather` 使用 `return_exceptions=True`，失败的 chunk 记录日志（包含失败数/总数）但不阻塞同层其他 chunk
- 结果按原始 index 写入 `chunk_results[idx]`
- 检查 gather 结果中的 Exception，记录 `logger.warning` 并将 `chunk_results[idx]` 保持为 None

*Phase 3: Bottom-up 汇总*
- 新增内部 helper `_derive_parent_summary(doc_title, children_abstracts)`：调用 `build_parent_summarization_prompt` → LLM completion → parse JSON
- 对每个 level（从深到浅），遍历该 level 中的所有 section 节点（`is_dir_chunk=True`）：收集直接子节点的 abstract → 调用 `_derive_parent_summary` → 调用 `self.update(section_uri, abstract=..., overview=..., meta={"topics": keywords})`
- **Partial failure guard**：如果某 section 节点超过半数子节点失败（`chunk_results[j] is None`），跳过该节点的 bottom-up 汇总，保留 heading text 作为 abstract，记录 warning
- 最后汇总 document parent：收集所有 `parent_index=-1` 的 chunk abstract → `_derive_parent_summary` → `self.update(doc_parent_uri, ...)`
- LLM 失败时 graceful degradation：父节点保留 `doc_title` 作为 abstract，overview 为空（与当前行为一致）
- 就地更新 `parent_ctx.abstract` 和 `parent_ctx.overview` 后返回

**Patterns to follow:**
- `batch_add()` 的 semaphore + gather 模式（`orchestrator.py:4822`）
- `_derive_layers_llm_completion` 的 retry 逻辑（已内置 3 次重试）

**Test scenarios:**
- Happy path: 3-section flat doc（all `parent_index=-1`）→ 3 chunks 并发 derive，parent 获得 bottom-up L0/L1
- Happy path: nested doc（## → ###）→ section 先创建，leaves 并发 derive，section 获得 bottom-up L0/L1，parent 获得 bottom-up L0/L1
- Happy path: 3-level nested doc（# → ## → ###）→ 验证拓扑层级计算和多层 bottom-up 级联正确
- Edge case: 单 chunk 文档 → 走现有 memory mode 快速路径，不触发并发逻辑
- Edge case: derive LLM 失败（mock 抛出异常）→ 失败 chunk 使用 fallback（截断 content 作为 abstract），其他 chunk 不受影响，parent bottom-up 仍使用可用的 children abstracts
- Edge case: 超过半数子节点失败 → 该 section 跳过 bottom-up 汇总，保留 heading text 作为 abstract
- Edge case: bottom-up LLM 失败 → parent 保留 doc_title，不 crash
- Integration: mock_llm 调用次数 = leaf chunk 数 + bottom-up 汇总次数（section 节点 + doc parent），entity sync 不贡献额外 LLM 调用（Unit 2b 修复后）
- Integration: parent record 的 abstract_json 包含非空 overview（当前为空）

**Verification:**
- QASPER 风格文档（10 chunks, `parent_index=-1`）总耗时 < 现有串行的 1/3
- Parent record 有 LLM 生成的 abstract 和 overview
- Section 节点有 LLM 生成的 abstract 和 overview
- 所有 chunk 的 `parent_uri` 指向正确的 parent/section URI
- 现有 `test_large_markdown_produces_multiple_records` 和 `test_small_doc_goes_to_memory` 仍通过
- Recall p50 不因本次改造劣化（R11）— 验证 `is_leaf=False` 记录不进入搜索结果

---

- [ ] **Unit 5: Increase client timeouts for store operations**

**Goal:** 将 store 操作的 client timeout 从 30s 提升到 300s，匹配并发后实际所需耗时。

**Requirements:** R9, R10

**Dependencies:** None（可与 Unit 1-4 并行实施）

**Files:**
- Modify: `plugins/opencortex-memory/src/tools.ts`
- Modify: `src/opencortex/http/client.py`

**Approach:**

*MCP client (`tools.ts:178`)*：
- `callProxyTool()` 中根据 tool name 选择 timeout：
  - `store`、`batch_store` → `AbortSignal.timeout(300000)`
  - 其他 → `AbortSignal.timeout(30000)`（保持不变）
- `lifecycle.ts:50` 保持 30s（context API 不涉及 document ingestion）

*Python HTTP client (`http/client.py`)*：
- 给 `store()` 方法（或 `_post()` 内部）添加 per-request timeout override
- store 操作使用 300s，其他操作保持 `_DEFAULT_TIMEOUT = 30.0`
- 参考 `httpx` 的 per-request timeout：`await self._client.post(url, ..., timeout=300.0)`
- **Retry 限制**：`_request()` 的 retry 循环（line 93-118）对 `ReadTimeout` 也会重试。300s timeout + 2 次 retry = 最坏 900s。修复：store 操作仅对 `ConnectError` 重试，不对 `ReadTimeout` 重试

**Patterns to follow:**
- `tools.ts` 中 `callProxyTool` 的 tool name routing 已有 `TOOLS[name]` 字典查找
- `http/client.py` 的 `httpx.AsyncClient` 支持 per-request `timeout` 覆盖

**Test scenarios:**
- Happy path: MCP store 调用使用 300s timeout（验证 AbortSignal 值）
- Happy path: MCP search 调用仍使用 30s timeout
- Happy path: Python client store 使用 300s timeout
- Edge case: Python client search 仍使用 30s timeout
- Edge case: store 操作 ReadTimeout 不触发 retry（避免 900s 阻塞）

**Verification:**
- store 操作在 30-300s 耗时内不超时
- 非 store 操作的 timeout 行为不变
- store ReadTimeout 不重试

---

### Conversation Units (6-9)

- [ ] **Unit 6: Add `superseded` search filter**

**Goal:** 在搜索路径中增加 `superseded` 过滤条件，为 Unit 8 的 final supersede 提供基础设施。

**Requirements:** R15, R16

**Dependencies:** None（纯搜索侧修改，独立于写入侧）

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py`
- Test: `tests/test_e2e_phase1.py`

**Approach:**
- 在 `_build_search_filter` 中增加条件：排除 `meta.superseded == True` 的记录。与现有 `is_leaf=True` 过滤条件 AND 组合
- 实现方式：在现有 filter 构建逻辑中追加一个 `must_not` 条件（或等效的 Qdrant filter DSL）。检查 `filter_translator.py` 是否支持 `must_not` / `ne` 操作
- 如果 `meta.superseded` 字段不存在（大部分现有记录），该条件应默认通过（不排除）。Qdrant 的 payload filter 对不存在的字段返回 `null`，`null != True` → 通过，符合预期
- 此 unit 仅添加 filter 条件。实际标记 `superseded=True` 的写入在 Unit 8

**Patterns to follow:**
- `hierarchical_retriever.py:_build_search_filter` 的现有 filter 构建模式
- `filter_translator.py` 的 VikingDB DSL → Qdrant Filter 翻译

**Test scenarios:**
- Happy path: 搜索正常返回非 superseded 记录
- Happy path: `meta.superseded=True` 的记录不出现在搜索结果中
- Edge case: `meta.superseded` 字段不存在的记录正常返回（不被误过滤）
- Edge case: `meta.superseded=False` 的记录正常返回
- Integration: 现有 E2E 测试不因新 filter 条件回归

**Verification:**
- 标记 `superseded=True` 的记录不出现在 search/recall 结果中
- 未标记的记录行为不变

---

- [ ] **Unit 7: Generate session-level summary at session end**

**Goal:** 在 session 结束时，从 merged records 的 abstract 底层汇总生成 session summary 对象（L1 overview → L0 abstract），写入为 `is_leaf=True` 的持久记录。

**Requirements:** R14, R17, R18

**Dependencies:** Unit 2（需要 `update()` 的 overview 支持）、Unit 3（复用 `build_parent_summarization_prompt`）、Unit 6（superseded filter 已就位）

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_context_manager.py`

**Approach:**

*8a. 等待 full recompose 完成*
- 当前 `_spawn_full_recompose_task` 是 fire-and-forget。Session summary 需要基于收敛后的 merged records，所以需要在 `_end()` 中等待 full recompose task 完成
- 在 `_end()` 中 `_spawn_full_recompose_task` 之后，添加 `await self._wait_for_full_recompose(sk)` — 复用或类似 `_wait_for_merge_task` 的等待模式
- 超时保护：`asyncio.wait_for(task, timeout=60.0)`，超时后仍继续生成 summary（基于当前 merged records）

*8b. 加载 merged records 并汇总*
- 调用 `_load_session_merged_records(session_id, source_uri)` 获取收敛后的 merged records
- 如果 merged records 数量 < 2，跳过 summary 生成（单条 merged 就是 summary）
- 收集所有 merged records 的 abstract → 调用 `build_parent_summarization_prompt(session_title, abstracts)` → LLM completion → parse JSON

*8c. 写入 session summary*
- URI 格式：`_session_summary_uri(tenant_id, user_id, session_id)` — 新增 helper
- 写入参数：`is_leaf=True`, `category="events"`, `context_type="memory"`, `meta={"layer": "session_summary", "session_id": session_id, "source_uri": source_uri, "merged_count": len(merged_records)}`
- abstract = summary JSON 的 `abstract`，content = summary JSON 的 `overview`（L1 作为可搜索内容）
- 成功后返回 summary URI 写入 `_end()` 返回值

*8d. Graceful degradation*
- LLM 调用失败：log warning，merged records 保持原状，`_end()` 正常返回（R18）
- session summary 写入失败：同上

**Patterns to follow:**
- `_persist_conversation_source()` 的写入模式（`manager.py:922`）
- `_derive_parent_summary` 的 LLM 调用模式（document Unit 4）
- `_spawn_full_recompose_task` + `_wait_for_merge_task` 的 task 管理模式

**Test scenarios:**
- Happy path: session with 3 merged records → `_end()` 后存在 1 个 `layer=session_summary` 的 `is_leaf=True` 记录，abstract 和 overview 非空
- Happy path: session summary 出现在搜索结果中（`is_leaf=True` 且无 `superseded` 标记）
- Edge case: session with 1 merged record → 不生成 summary
- Edge case: session with 0 merged records（空 session）→ 不生成 summary
- Error path: LLM summary 生成失败 → `_end()` 正常返回，无 summary 记录，merged records 不受影响
- Error path: full recompose 超时 → summary 基于超时前可用的 merged records 生成
- Integration: session summary 的 abstract 包含了 merged records 的核心内容（mock LLM 验证输入）

**Verification:**
- session 结束后存在 `layer=session_summary` 记录
- summary 记录 `is_leaf=True`，出现在正常搜索结果中
- summary 生成失败不影响 session 正常关闭
- memory mode session（无 merged records）不触发 summary 生成

---

- [ ] **Unit 8: Final supersede — merged records exit search surface after summary**

**Goal:** Session summary 写入成功后，将该 session 的 merged records 标记为 `superseded=True`，使其退出主搜索面。

**Requirements:** R15, R16, R18

**Dependencies:** Unit 6（搜索过滤已就位）、Unit 7（summary 已生成）

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_context_manager.py`

**Approach:**
- 在 Unit 8 的 session summary 写入成功后，遍历 merged records 并调用 `self._orchestrator.update(uri, meta={"superseded": True})` 标记
- 标记失败不阻塞 `_end()` — 用 `try/except` 包裹每个 update，部分标记失败记 warning
- 仅在 summary 写入成功后才执行标记（R18 graceful degradation：summary 失败 → merged records 保持原状）
- 不标记 `layer=conversation_source` 记录（source 是 traceability 基础，永不 supersede）
- 不标记 `layer=session_summary` 记录（summary 本身不应被 supersede）

**Patterns to follow:**
- `_merge_buffer` 中的 superseded merged cleanup 模式（`manager.py:1889-1907`）
- `update()` 的 meta 更新接口

**Test scenarios:**
- Happy path: session with 3 merged records → summary 成功 → 3 条 merged records 都有 `meta.superseded=True` → 搜索不返回它们 → 搜索返回 session summary
- Edge case: summary 生成失败 → merged records 无 `superseded` 标记 → 搜索仍返回 merged records（降级行为）
- Edge case: 部分 merged record update 失败 → 成功标记的退出搜索面，失败的保留
- Edge case: 已有 `superseded=True` 的 merged record（幂等性）→ re-update 是 no-op
- Integration: session summary 在搜索中取代了原有的 merged records（搜索同一 session 话题，返回 summary 而非碎片 merged records）

**Verification:**
- session end 后搜索该 session 话题返回 session summary 而非 merged records
- merged records 在 Qdrant 中仍存在（soft delete），但搜索不可见
- 降级场景下 merged records 仍可搜索

---

### Cross-cutting Units (9-10)

- [ ] **Unit 9: Align L0/L1 generation with OpenViking §4.5 — LLM co-generates abstract**

**Goal:** `_derive_layers` prompt 输出增加 `abstract` 字段，LLM 同时生成 L0 abstract + L1 overview（三层同源），替代当前纯代码截取 overview 首句的方式。

**Requirements:** R12

**Dependencies:** None（独立修改，所有 mode 都经过 `_derive_layers`）

**Files:**
- Modify: `src/opencortex/prompts.py`
- Modify: `src/opencortex/orchestrator.py`
- Test: `tests/test_e2e_phase1.py`

**Approach:**

*9a. 修改 prompt*
- `build_layer_derivation_prompt`（`prompts.py:169`）：JSON 输出增加 `"abstract"` 字段
- Rules 增加：`abstract: 一行简洁摘要（≤200字符），概括核心事实，适合搜索和去重。必须是完整句子，不是 overview 的截断。`
- 放在 `overview` 之后、`keywords` 之前

*9b. 修改 `_derive_layers` 解析*
- 正常路径（`orchestrator.py:1310-1358`）：从 LLM JSON 读取 `data.get("abstract", "")`
- Chunked 路径（`orchestrator.py:1249-1303`）：同样读取 `result.get("abstract", "")`
- 如果 LLM 返回了非空 abstract 且 `user_abstract` 为空，使用 LLM abstract；否则走现有逻辑（`_derive_abstract_from_overview` 作为 fallback）
- `_derive_abstract_from_overview` 保持不变，仅降级为 no-LLM 和 LLM-未返回-abstract 的 fallback

*9c. 向后兼容*
- 老版本 LLM 可能不返回 `abstract` 字段 → `data.get("abstract", "")` 返回空 → 自动 fallback 到 `_derive_abstract_from_overview`
- `user_abstract` 非空时仍优先使用 user 提供的值（现有行为不变，`orchestrator.py:1947`）

**Patterns to follow:**
- `_derive_layers` 中 `overview`/`keywords`/`entities` 的现有解析模式

**Test scenarios:**
- Happy path: LLM 返回含 `abstract` 的 JSON → L0 使用 LLM abstract 而非 overview 首句截断
- Happy path: user 提供了 abstract → 忽略 LLM abstract（现有优先级不变）
- Edge case: LLM 返回 JSON 无 `abstract` 字段 → fallback 到 overview 首句截断（向后兼容）
- Edge case: LLM 返回空 `abstract: ""` → fallback 到 overview 首句截断
- Edge case: chunked 路径（content > 4000 chars）也正确读取 LLM abstract
- Integration: memory mode add + document mode derive 都使用 LLM 生成的 abstract

**Verification:**
- 新写入的记录 L0 abstract 是 LLM 生成的完整摘要，非 overview 首句截断
- 现有 user_abstract 优先逻辑不受影响
- 无 LLM 配置时行为不变

---

- [ ] **Unit 10: Raise conversation merge token threshold to 2000**

**Goal:** 将 conversation merge 触发阈值从 1000 tokens 提升到 2000 tokens，给语义分段更大的窗口。

**Requirements:** R13

**Dependencies:** None（独立修改）

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_context_manager.py`

**Approach:**
- `manager.py:1509`：`if buffer.token_count >= 1000:` → `>= 2000`
- `manager.py:1741`：`if not flush_all and buffer.token_count < 1000:` → `< 2000`
- 两处同步修改，不引入配置项

**Patterns to follow:**
- 现有硬编码值的使用方式

**Test scenarios:**
- Happy path: 1500 tokens 不触发 merge（之前会触发）
- Happy path: 2000 tokens 触发 merge
- Edge case: `flush_all=True` 时不受阈值限制（session end 时 flush 所有剩余）
- Integration: 现有 test_context_manager 测试中涉及 merge 的场景仍通过（可能需要调整 mock 数据量）

**Verification:**
- merge 仅在 buffer >= 2000 tokens 时触发
- session end flush_all 行为不变

## System-Wide Impact

### Document

- **Interaction graph:** `_add_document()` 修改影响所有 document mode ingestion。`update()` 接口扩展是向后兼容的（新参数有默认值）。Timeout 修改影响所有 MCP store/batch_store 调用和 Python client store 调用。
- **Error propagation:** 并发 chunk 处理中的单个失败不阻塞同层其他 chunk（`return_exceptions=True`）。Bottom-up LLM 失败 graceful 降级（父节点保留 doc_title）。
- **State lifecycle risks:** `chunk_results` 预分配避免了并发 append 的竞态。`gather` 保证所有 level N task 完成后才进入 level N+1。Parent record 的 write-after-write（先创建空 L0/L1，后 update）依赖 `update()` 的 upsert 语义。
- **API surface parity:** `update()` 签名变更对外部调用者透明（新参数有默认值 None）。MCP tool 接口不变。HTTP API 不变。

### Conversation

- **Interaction graph:** `_end()` 扩展增加 session summary 生成和 supersede 标记。`_build_search_filter` 增加 `superseded` 过滤。
- **Error propagation:** Session summary LLM 失败不阻塞 `_end()`。Supersede 标记部分失败 → 部分 merged records 保留搜索可见性。
- **State lifecycle risks:** full recompose 必须在 session summary 之前完成（否则 summary 基于未收敛的 merged records）。Supersede 标记必须在 summary 写入成功之后（否则 merged records 退出搜索面但无替代）。这两个顺序约束通过 `_end()` 中的串行 await 保证。
- **API surface parity:** `_end()` 返回值增加 `session_summary_uri` 字段（additive）。搜索 API 行为变化：superseded merged records 不再出现（用 session summary 替代）。

### Cross-cutting

- **Performance constraint (R11):** Recall p50 必须 < 1s。Document 改造仅影响 write path。Conversation supersede filter 增加一个 AND 条件到搜索 filter — 对 Qdrant payload filtering 的性能影响可忽略。基线参考：当前 document mode recall p50 = 2125-2969ms（已超标，非本次引入），memory mode p50 = 98ms。
- **Integration coverage:** 需验证 document mode E2E（parse → concurrent derive → bottom-up → Qdrant records with L0/L1）。需验证 conversation mode E2E（merge → session summary → supersede → 搜索返回 summary）。需验证 memory mode 不受影响。
- **L0/L1 生成变更（R12）：** `_derive_layers` prompt 新增 `abstract` 输出字段，影响所有经过 LLM derive 的路径（memory/document/conversation merge）。`_derive_abstract_from_overview` 保留作为 fallback。LLM 未返回 abstract 时行为不变。
- **Merge 阈值变更（R13）：** 1000→2000 影响 conversation mode 的 merge 触发频率（降低一半）。Session end 的 `flush_all=True` 不受阈值限制。
- **Unchanged invariants:** MarkdownParser chunking 不改。Memory mode 的 `add()` 路径不改（仅 derive 输出质量变化）。`prepare/commit/end` 生命周期不改。Merged leaves 在 session 活跃期间仍是主搜索面。

## Risks & Dependencies

### Document

| Risk | Mitigation |
|------|------------|
| LLM provider 在并发下限流（429） | `_derive_layers_llm_completion` 内置 3 次 retry（0/0.35s/0.8s）；semaphore 限制并发度为 3 |
| `chunk_results` 索引错位导致错误 parent_uri | 预分配列表 + gather 按提交顺序返回 + 按 level 串行化跨层依赖 |
| Bottom-up LLM 失败导致 parent record 无 L0/L1 | Graceful degradation：保留 doc_title/heading text 作为 abstract，与当前行为一致（不会更差） |
| `update()` entity sync 触发多余 LLM 调用 | Unit 2b：`is_leaf=False` 记录跳过 entity sync（review 发现） |
| `update()` CortexFS write 清除 parent L2 内容 | Unit 2c：用 `next_content` 替代 `content or ""`（review 发现） |
| `update()` CortexFS 不写 overview | Unit 2d：`write_context` 传 `overview=next_overview`（review 发现） |
| `update()` 扩展引入回归 | 新参数默认 None，不影响现有调用者；添加专门测试 |
| 300s timeout + retry = 900s 阻塞 | store 操作对 ReadTimeout 不重试（Unit 5） |
| 超半数子节点失败产生误导性 parent 摘要 | Partial failure guard：>50% 失败时跳过 bottom-up（Unit 4） |

### Conversation

| Risk | Mitigation |
|------|------------|
| Full recompose 超时导致 summary 基于未收敛数据 | 60s 超时保护 + 基于当前可用 merged records 生成（降级但可用）|
| Session summary LLM 质量不足 | 复用 document parent summarization prompt（已验证），输入是 merged records 的 LLM 生成 abstract（质量高于原文截断）|
| Supersede 标记后用户想恢复旧 merged records | Soft delete 保留数据，移除 `superseded` 标记即可恢复 |
| `_end()` 延迟增加（等 full recompose + LLM summary） | Full recompose 已是 async task（现有代码 fire-and-forget），改为 await 增加延迟但保证数据一致性。Summary LLM 调用 ~5-10s，对 session end 可接受 |
| `superseded` filter 误过滤非 conversation 记录 | `superseded` 字段仅由 Unit 8 写入 conversation merged records，其他记录无此字段 → filter 默认通过 |

## Sources & References

- **Origin document:** [document-ingestion-parallel-derive-requirements.md](docs/brainstorms/2026-04-17-document-ingestion-parallel-derive-requirements.md)
- **Related plan:** [conversation semantic merge plan](docs/plans/2026-04-16-004-refactor-conversation-semantic-merge-plan.md) — plan-004 覆盖 semantic segmentation，本计划覆盖 anchor 清理 + session summary + final supersede
- Related code: `orchestrator.py:_add_document()`, `orchestrator.py:batch_add()`, `manager.py:_end()`, `manager.py:_delete_immediate_families()`, `prompts.py`, `config.py`
- Related design: `docs/design/2026-04-14-opencortex-openviking-borrowable-retrieval-optimization.md` §4.3-§4.4 (conversation), §4.6 (document bottom-up L0/L1)
- Related tests: `tests/test_document_mode.py`, `tests/test_ingestion_e2e.py`, `tests/test_context_manager.py`
