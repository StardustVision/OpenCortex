---
title: "fix: Plan 006 adversarial review findings"
type: fix
status: active
date: 2026-04-17
origin: docs/plans/2026-04-16-006-feat-minimum-cost-retrieval-fact-points-plan.md
---

# fix: Plan 006 adversarial review findings

## Overview

Plan 006（minimum-cost retrieval with fact_points）通过 170 tests。Codex adversarial review 发现 5 个需 ship 前修复的问题：3 个 P0 + 2 个 P1。

P0：`update()` 抹 fact_points、scope filter 不覆盖派生面、cascade 测试用假 storage。
P1：embed_batch 长度错位、score_threshold 语义偏移。

## Problem Frame

Plan 006 主体正确，但 integration 边界有三类漏洞：

1. **写路径不对称**：`add()` 注入 fact_points，`update()` 不注入 → 每次 update 静默删空 fact_points
2. **scope filter 不完整**：三层搜索只给 leaf search 加 scope 约束，anchor/fp search 和 batch leaf load 未加 → 跨 container 泄漏
3. **测试假绿**：InMemoryStorage 用 `startswith`，Qdrant MatchText 是 tokenized substring → cascade 行为未真实覆盖

P1：`CachedEmbedder.embed_batch` 对 None 过滤导致 output len != input len，下游 zip 错位污染向量；URI_DIRECT_PENALTY=0.15 全局下移 score，固定 `score_threshold` 的 caller 静默退化。

## Requirements Trace

- F1 (ADV-001): `update()` 路径保留 fact_points
- F2 (ADV-002): scope filter 覆盖 anchor/fp search 和 URI batch load
- F3 (ADV-004): cascade 行为在真实 Qdrant 语义下验证
- F4 (ADV-005): embed_batch 严格保持 input/output 长度
- F5 (ADV-006): score_threshold 语义向后兼容或显式记录偏移

## Scope Boundaries

- In scope: 修复 5 个 finding，加回归测试
- Out of scope: ADV-003 (ACL 快照 policy)、ADV-007 (并发 race)、ADV-008 (异常可观测性)、ADV-009 (cost 放大) — 列入 follow-up

### Deferred to Separate Tasks

- 派生记录 ACL re-sync 机制（ADV-003）
- 并发 recomposition 锁/版本控制（ADV-007）
- 三层搜索 per-surface 失败 telemetry（ADV-008）
- should_recall 移除后的 cost 负载测试（ADV-009）

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/orchestrator.py:2304-2438` — `update()` 调 `_derive_layers` 但只读 entities/keywords
- `src/opencortex/orchestrator.py:2013-2016` — `add()` 显式注入 `layers.get('fact_points', [])`
- `src/opencortex/orchestrator.py:3042-3108` — `_execute_object_query` 三层 filter 构建
- `src/opencortex/orchestrator.py:3188-3208` — `missing_uris` batch load 用 `search_filter`（缺 leaf_filter）
- `src/opencortex/storage/qdrant/adapter.py:341-365` — `remove_by_uri` 用 MatchText
- `src/opencortex/storage/qdrant/filter_translator.py:180-206` — `op=prefix` 也映射到 MatchText
- `src/opencortex/models/embedder/cache.py:75-103` — `embed_batch` 最后 `[r for r in results if r is not None]` 过滤

## Key Technical Decisions

- **update() 对称注入**：在 `_build_abstract_json` 或 `update()` 注入 `layers["fact_points"]`，走与 `add()` 相同的 sync 路径
- **Scope filter 共享 base**：把 `leaf_filter` 的 scope 部分（parent_uri/session_id/source_doc_id）提取为 `scope_only_filter`，三层 search 和 missing_uris load 都加入。`is_leaf=True` 继续只加 leaf search
- **Cascade 测试双轨**：InMemoryStorage 测试保留（快速），新增一套用 embedded Qdrant 的 integration 测试覆盖 MatchText 行为
- **embed_batch 长度不变契约**：不过滤 None，直接返回 `results`，None 位置替换为零向量 `EmbedResult` placeholder。调用方自行处理零向量（当前 `_sync_anchor_projection_records` 已有零向量 fallback）
- **score_threshold 偏移注明**：在 uri_path_scorer docstring 和 `_execute_object_query` 注释里显式记录 URI path score 相对 vector cosine 的偏移上限（direct path -0.15，anchor path -0.05，fp path -0.025~-0.05）。不改算法，仅文档化

## Open Questions

### Resolved During Planning

- **scope_only_filter 怎么构建？** 复用现有 `leaf_filter` 条件但去掉 `is_leaf=True`。已有变量 `parent_uris`/`session_ids`/`doc_ids` 直接复用
- **embed_batch None 位置怎么处理？** 返回零向量 `EmbedResult(dense_vector=[0.0]*dim)` placeholder。dim 通过 `self._inner.get_dimension()` 获取

### Deferred to Implementation

- cascade integration test 用现有 embedded Qdrant fixture 还是专开？看测试运行时成本

## Implementation Units

- [ ] **Unit 1 (P0): update() 路径注入 fact_points**

**Goal:** `update()` 和 `_merge_into()` 触发 `_sync_anchor_projection_records` 时带上 fact_points，不再静默抹除。

**Requirements:** F1

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/orchestrator.py` (`update()` 或 `_build_abstract_json()`)
- Test: `tests/test_context_manager.py`

**Approach:**
- 在 `update()` 调用 `_derive_layers` 后，除了读 `entities`/`keywords`，也读 `fact_points`
- 把 `fact_points` 注入 `abstract_json`（与 `add()` 的 line 2013-2016 对称）
- `_merge_into` 如果走同一路径，自动受益；若走独立路径，也要补注入

**Test scenarios:**
- Happy path: `add()` 创建含 fact_points 的 leaf → `update(uri, abstract=...)` → 断言 `/fact_points/*` 仍存在且内容已更新
- Edge case: `update()` 传入 `abstract=None content=None` → fast path 不触发 re-derive → fact_points 应保留不变（不删也不更新）
- Edge case: `_merge_into` 合并两个 memory → 合并后 leaf 的 fact_points 来自新 `_derive_layers` 结果
- Regression: 原有 update() 测试仍通过

**Verification:**
- `orchestrator.update()` 后查 `retrieval_surface="fact_point"` + `parent_uri=leaf_uri` 数量非零（前提是 LLM 返回了 fact_points）

---

- [ ] **Unit 2 (P0): scope filter 覆盖 anchor/fp search + URI batch load**

**Goal:** CONTAINER_SCOPED / SESSION_ONLY / DOCUMENT_ONLY 三种 scope 下，anchor/fp search 和 batch leaf load 正确过滤到 scope 内。

**Requirements:** F2

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/orchestrator.py` (`_execute_object_query`)
- Test: `tests/test_context_manager.py`

**Approach:**
- 构建 `scope_only_filter`：从现有 leaf_filter 逻辑提取 parent_uri/session_id/source_doc_id 约束，去掉 `is_leaf`
- anchor/fp 记录已继承这些字段（见 `_anchor_projection_records`、`_fact_point_records`）→ scope_only_filter 对它们同样有效
- `anchor_filter_merged` = `search_filter` + `start_point_filter` + `scope_only_filter` + `retrieval_surface="anchor_projection"`
- `fp_filter_merged` = `search_filter` + `start_point_filter` + `scope_only_filter` + `retrieval_surface="fact_point"`
- `missing_filter` = `search_filter` + `scope_only_filter` + `{uri in missing_uris}` + `is_leaf=True`

**Test scenarios:**
- Happy path: CONTAINER_SCOPED，query 命中 out-of-scope 的 fp → leaf 不出现在结果
- Happy path: SESSION_ONLY，target session_id=A，其他 session 的 anchor/fp 不参与路径打分
- Happy path: DOCUMENT_ONLY，target doc_id=X，其他 doc 的 anchor/fp 不参与
- Edge case: GLOBAL scope（无 scope 约束）→ 行为不变，所有 anchor/fp 参与
- Regression: 现有三层搜索测试仍通过

**Verification:**
- CONTAINER_SCOPED 下，`_execute_object_query` 返回的 leaf uris 全部 parent_uri ∈ container 集合

---

- [ ] **Unit 3 (P0): Cascade integration test 覆盖 Qdrant MatchText 行为**

**Goal:** 真实 Qdrant 语义下验证 fact_point/anchor cascade delete 行为，避免 InMemoryStorage 假绿。

**Requirements:** F3

**Dependencies:** None

**Files:**
- Create: `tests/test_cascade_qdrant_integration.py`
- 可能 Modify: `src/opencortex/storage/qdrant/adapter.py` (如果发现真实 bug)

**Approach:**
- 使用现有的 embedded Qdrant fixture（`QdrantStorageAdapter` with `memory` mode）
- 测试写入 sibling leaves 共享 URI tokens (e.g. UUID 前缀相同)
- 创建 fact_points/anchors 于两个 sibling
- 删除 leaf A → 断言 leaf A 的 fp/anchor 被删，leaf B 的 fp/anchor 保留
- 测试 `_delete_derived_stale` 在 sibling 共享 tokens 时只删目标

**Test scenarios:**
- Happy path: 两个 sibling leaves `opencortex://t/u/mem/leafA_uuid1` 和 `opencortex://t/u/mem/leafB_uuid2`，删 A 的 fp 不影响 B
- Edge case: URI token 碰撞（人为构造 tokens 重叠的 URI）→ 验证 over-delete 不发生
- Edge case: 长 URI 中有短 token（min_token_len=2 过滤边界）→ 验证 under-delete 不发生
- Integration: `orchestrator.remove(leaf_uri)` → 查 Qdrant 验证 `/fact_points/*` 和 `/anchors/*` 全清，siblings 不受影响

**Verification:**
- Qdrant 语义下 cascade 行为与 InMemoryStorage 语义一致（或文档化差异）
- 如发现真实 bug，修 adapter 的 `remove_by_uri`（改用更严格的匹配，如 keyword 字段 + prefix match）

---

- [ ] **Unit 4 (P1): CachedEmbedder.embed_batch 严格保持长度**

**Goal:** `embed_batch(N texts)` 必定返回 `N` 个 `EmbedResult`，缺失位置填零向量，不过滤 None。

**Requirements:** F4

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/models/embedder/cache.py` (`embed_batch`)
- Test: `tests/test_cached_embedder.py`

**Approach:**
- 当 `self._inner.embed_batch(miss_texts)` 返回 len < miss_texts 时，不再静默
- 选项 A: 抛 ValueError（严格契约）
- 选项 B: 用零向量 `EmbedResult(dense_vector=[0.0]*dim)` 填补缺失位置 → 推荐，与现有 "embed 失败退化到零向量" 语义一致
- 移除 line 103 的 `[r for r in results if r is not None]` 过滤
- 保证 `len(output) == len(input)`

**Test scenarios:**
- Happy path: inner.embed_batch 返回完整 N 个 → output 正确
- Edge case: inner.embed_batch 返回 N-1（模拟部分失败）→ output 仍为 N 个，最后一个是零向量
- Edge case: inner.embed_batch 返回 []（全失败）→ output 为 N 个零向量
- Edge case: partial cache hit + partial inner 失败 → 命中位置用 cache，miss+失败位置用零向量
- Regression: 现有 embed_batch 测试仍通过

**Verification:**
- 对任意 N 个 input，`len(cached.embed_batch(texts)) == N`

---

- [ ] **Unit 5 (P1): score_threshold 语义偏移文档化**

**Goal:** 明确 URI path scoring 引入的 score 偏移（direct -0.15、anchor -0.05、fp -0.025~-0.05），避免 score_threshold caller 静默退化。

**Requirements:** F5

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/retrieve/uri_path_scorer.py` (module docstring)
- Modify: `src/opencortex/orchestrator.py` (`_score_object_record` 注释)
- Test: `tests/test_uri_path_scorer.py` (score 偏移断言)

**Approach:**
- 在 `uri_path_scorer.py` module docstring 加说明：
  - URI path score = `1.0 - min_cost`，相对 cosine similarity 的偏移由 penalty/hop 决定
  - direct path: score = cosine - URI_DIRECT_PENALTY (=0.15)
  - anchor path: score = cosine - URI_HOP_COST (=0.05)
  - fp path (high confidence): score = cosine - URI_HOP_COST * HIGH_CONFIDENCE_DISCOUNT (=0.025)
- `_score_object_record` 注释：`score_threshold` caller 应按需调整（减 0.15 for direct-hit-dominant scenarios）
- 添加 test 断言偏移数值：cosine=0.82 + direct path + URI_DIRECT_PENALTY=0.15 → score=0.67
- Post-ship 任务：ramp URI_DIRECT_PENALTY 到 0.30 前跑 benchmark

**Test scenarios:**
- Happy path: leaf cosine=0.82，只有 direct path → URI path score=0.67（断言 0.67 ± epsilon）
- Happy path: leaf cosine=0.82，anchor path distance=0.20 → anchor path cost=0.25，score=0.75
- Happy path: leaf fp_distance=0.05（< 0.10 threshold）→ hop 折扣 → score=0.925
- Regression: 现有 uri_path_scorer 16 tests 仍通过

**Verification:**
- Test 显式锁定偏移数值，未来改动 URI_DIRECT_PENALTY 时测试必然失败 → 强制 reviewer 知道语义变化

## System-Wide Impact

- **Interaction graph**: `update()` 走 `_derive_layers` → `_build_abstract_json` → `_sync_anchor_projection_records`，补 fact_points 注入
- **Error propagation**: `embed_batch` 长度契约严格后，下游 zip 不再错位。异常场景（inner 失败）通过零向量 fallback 表达
- **State lifecycle risks**: 修复后 `update()` 不再误删 fact_points → 高频 update/merge_into 场景数据质量恢复
- **API surface parity**: 无 API 变更。`score_threshold` 语义记录为已知偏移，不引入 breaking change
- **Unchanged invariants**: Plan 006 核心架构（三层搜索、minimum-cost 打分、should_recall 移除）不变

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Unit 3 integration test 运行时长增加 | 只跑 cascade 关键场景（3-4 条），复用 embedded Qdrant fixture |
| Unit 4 改长度契约可能影响其他 caller | grep 所有 `embed_batch` 调用点，确认零向量 placeholder 语义可接受 |
| Unit 5 只文档化不改算法 → 不解决 caller 行为变化本身 | Post-ship 加 benchmark gate，caller 自行调整 threshold |

## Sources & References

- **Origin plan:** [docs/plans/2026-04-16-006-feat-minimum-cost-retrieval-fact-points-plan.md](docs/plans/2026-04-16-006-feat-minimum-cost-retrieval-fact-points-plan.md)
- **Adversarial review findings:** Codex adversarial-review (ADV-001 through ADV-009)
- **Plan 006 commits to fix:** 5ddb6bc..HEAD (9 commits)
