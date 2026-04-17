---
date: 2026-04-18
sequence: "001"
status: active
scope: fix
title: "Fix Conversation Merge Raw Content + Anchor-Based Clustering"
origin: docs/brainstorms/2026-04-16-conversation-semantic-merge-traceability-requirements.md
supersedes: null
baseline: docs/plans/2026-04-16-005-refactor-conversation-overview-first-hard-anchors-plan.md
---

# Fix Conversation Merge Raw Content + Anchor-Based Clustering

## Problem Frame

Plan 005 完成后，LoCoMo 基准验证暴露了三个遗留问题：

1. **Online tail recompose 读 Qdrant L1 摘要而非 CortexFS L2 原文**。`_build_recomposition_entries` 对 tail_records 调用 `_record_text(record)` → Qdrant payload 不含 `content` 字段（`Context.to_dict()` 不包含它）→ fallback 到 `overview`/`abstract`（LLM 产物）。首次 merge 的 content.md 实际已包含原文（`orchestrator.add(content=combined)` 写入 raw text），但后续 tail recompose 不读 CortexFS → 混入 LLM 摘要 → 腐化级联：每轮 recompose 中 tail 部分越来越远离原文。

2. **Full recompose 使用 token 预算切分，无语义感知**。`_build_recomposition_segments` 只按 `_SEGMENT_MAX_TOKENS` / `_SEGMENT_MAX_MESSAGES` + time_refs 不重叠做顺序切分。不利用 merged records 已有的 entities/topics/time_refs 元数据。结果：长会话仍可能把不同主题压到同一 segment，或在语义连贯点强制切断。

3. **`_delete_immediate_families` 只清 Qdrant，不清 CortexFS**。Qdrant 记录被删后，对应的 CortexFS 目录（`.abstract.md` / `.overview.md` / `content.md`）成为孤儿，磁盘持续积累。

## Scope

**In scope**:
- 修复 merge 和 full_recompose 中的 content 来源，确保 content.md 保留原始消息文本
- 将 full_recompose 的切分逻辑替换为基于 anchor 元数据的 Jaccard 语义聚类
- 在 `_delete_immediate_families` 中增加 CortexFS 目录清理

**Out of scope**:
- Recall 路径、IntentRouter、HierarchicalRetriever（不修改）
- 新的检索通道或层级结构
- Online merge 的触发阈值调整
- Document mode 或 memory mode
- L0/L1 合约变更（LLM derive 仍生成 abstract/overview/keywords — 只是输入变成原文而非摘要）

## Requirements Trace

| Req | Origin | Coverage |
|-----|--------|----------|
| R1-R2 | Semantic merge shape | Unit 2: anchor-based clustering 替代 token-budget 切分 |
| R3 | 保持消息原顺序 | Unit 2: 按 msg_start 排序后聚类，不做全局重排 |
| R4 | 少量可解释信号 | Unit 2: entities + topics + time_refs Jaccard，零 LLM |
| R5 | 每段语义聚焦 | Unit 2: 同 anchor 主题的 records 归同一组 |
| R6-R9 | 原文溯源 | Unit 1: content.md 保留原始消息，msg_range 精确 |
| R10 | 检索可用性 | L0/L1 仍由 LLM derive 生成，检索使用 abstract embedding |
| R11-R13 | 检索质量 | Unit 2: 更聚焦的 segments → 更精确的 anchors |
| R15-R16 | 简单性 | 不引入新层级/新检索通道，复用现有生命周期 |
| R17 | Transcript 不承担排序 | Merged leaf 仍是主检索对象 |

## Key Decisions

1. **Tail records 读 CortexFS L2 原文**：online merge 中 tail_records 当前用 `_record_text(record)` 取 Qdrant payload，但 Qdrant 不存 `content`（`Context.to_dict()` 排除它），所以 fallback 到 `overview`/`abstract`（LLM 摘要）。CortexFS L2 content.md 已包含原始消息（`orchestrator.add(content=combined)` 写入 raw text）。改为读 CortexFS L2 content.md 切断腐化级联。若 CortexFS 读失败，fallback 到 `_record_text(record)`。

2. **Anchor-based Jaccard 聚类替代 token-budget 切分**：full_recompose 已有每条 merged record 的 `entities`、`topics`、`time_refs` 元数据（via `_segment_anchor_terms` + `_segment_time_refs`）。用 Jaccard 相似度做 record-level 聚类：相邻 records anchor 重叠高 → 归同组。零 LLM 开销，无上下文窗口限制。token 上限仅作为 hard cap 防止单 segment 过大。

3. **Online merge 保留 token-budget 顺序切分**：online merge 是热路径，需要低延迟。anchor-based 聚类只在 full_recompose（async 后台）中使用。online merge 继续用 `_build_recomposition_segments` 的顺序切分，但输入文本改为原文。

4. **CortexFS 清理使用 fire-and-forget**：`_delete_immediate_families` 增加 CortexFS `rm(uri, recursive=True)` 调用，失败仅 log warning 不阻塞主流程。

5. **L0/L1 合约不变**：`orchestrator.add()` 仍对 content 做 `_derive_layers()` 生成 abstract/overview/keywords。区别是输入从"LLM 摘要"变成"原始消息文本"→ derive 质量更高，不再双重摘要。

## Implementation Units

### Unit 1: Fix merge content to preserve raw messages

**Goal**: 确保 online merge 的 `_build_recomposition_entries` 对 tail_records 使用 CortexFS L2 原文，而非 Qdrant L1 摘要。

**Files**:
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_conversation_merge.py`

**Approach**:
1. 在 `_build_recomposition_entries` 中，tail_records 部分增加 CortexFS L2 读取逻辑（参考 `_run_full_session_recomposition` lines 1636-1646 的 `_read_l2` 模式）
2. 将方法改为 `async def _build_recomposition_entries()`（当前是 sync）以支持 CortexFS async read
3. 更新 `_merge_buffer` 中的调用点加 `await`
4. L2 读取失败时 fallback 到 `_record_text(record)`

**Patterns to follow**: `_run_full_session_recomposition` lines 1636-1646 — 批量 CortexFS L2 读取模式。

**Test scenarios**:
- Tail record 有 CortexFS L2 内容 → entries 使用 L2 原文
- Tail record CortexFS 读取失败 → fallback 到 Qdrant overview
- Immediate records 仍使用 `snapshot.messages` 原文（回归保护）
- 合并后 `orchestrator.add()` 收到的 content 是原始消息文本

**Verification**: merge 后检查 CortexFS content.md 内容包含 `[timestamp] [speaker]:` 格式的原始消息，而非 LLM 叙述体摘要。

### Unit 2: Anchor-based semantic clustering for full_recompose

**Goal**: 替换 full_recompose 中的 token-budget 顺序切分为基于 anchor 元数据 Jaccard 相似度的语义聚类。

**Files**:
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_conversation_merge.py`

**Approach**:

新增 `_build_anchor_clustered_segments(entries)` 方法，替代 full_recompose 中的 `_build_recomposition_segments` 调用：

1. **提取 anchor 集合**：每条 entry 已有 `anchor_terms` (entities + topics) 和 `time_refs`。合并为 `anchor_set = anchor_terms ∪ time_refs`。
2. **顺序扫描 + Jaccard 分组**：按 `msg_start` 顺序遍历 entries。当前 group 的 anchor 集合与下一条 entry 的 Jaccard 相似度 < 阈值 → 切开新组。阈值建议 `0.15`（低阈值 = 只要有少量共享 anchor 就归同组）。
3. **Hard caps**：单组 token 超过 `_RECOMPOSE_SEGMENT_MAX_TOKENS`（建议 3000）时强制切分。单组消息数超过 `_RECOMPOSE_SEGMENT_MAX_MESSAGES`（建议 30）时强制切分。
4. **空 anchor 处理**：若 entry 的 anchor_set 为空，归入前一组（避免孤立）。

算法（伪码）：
```
groups = []
current_group = [entries[0]]
current_anchors = entries[0].anchor_set

for entry in entries[1:]:
    if entry.anchor_set is empty:
        current_group.append(entry)  # 归前一组
        continue
    jaccard = |current_anchors ∩ entry.anchor_set| / |current_anchors ∪ entry.anchor_set|
    if jaccard >= threshold AND tokens < hard_cap AND messages < msg_cap:
        current_group.append(entry)
        current_anchors = current_anchors ∪ entry.anchor_set
    else:
        groups.append(current_group)
        current_group = [entry]
        current_anchors = entry.anchor_set

groups.append(current_group)
```

5. 在 `_run_full_session_recomposition` 中将 `segments = self._build_recomposition_segments(entries)` 替换为 `segments = self._build_anchor_clustered_segments(entries)`。
6. Online merge 路径不变 — `_merge_buffer` 继续使用 `_build_recomposition_segments`。

**Patterns to follow**: `_segment_anchor_terms` (line 1091) — anchor 提取逻辑；`_build_recomposition_segments` (line 1298) — 段输出格式（通过 `_finalize_recomposition_segment`）。

**Test scenarios**:
- 3 条 records: 2 条共享 entities ("Alice", "Hangzhou")，1 条不同 ("Bob", "Shanghai") → 产生 2 个 segments
- 所有 records 共享相同 anchors → 产生 1 个 segment（除非超 hard cap）
- 单条 record tokens 超过 hard cap → 该 record 独立成 segment
- 空 anchor 的 entry 归入前一组
- 零 entries → 返回空列表
- 输出 segment 格式与 `_finalize_recomposition_segment` 一致（messages, msg_range, immediate_uris, superseded_merged_uris）

**Verification**: full_recompose 对多主题会话产生按主题聚焦的 segments，每个 segment 的 anchor 内聚度高于 token-budget 切分。

### Unit 3: CortexFS orphan directory cleanup

**Goal**: `_delete_immediate_families` 在删除 Qdrant 记录后同时清理 CortexFS 目录。

**Files**:
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_conversation_merge.py`

**Approach**:
1. 获取 `CortexFS` 实例：`fs = getattr(self._orchestrator, "_fs", None)`
2. 在每个 URI 的 Qdrant `remove_by_uri` 后，调用 `fs.rm(uri, recursive=True)`
3. CortexFS rm 失败仅 log warning，不抛异常
4. 若 `fs` 为 None（测试环境无 CortexFS），跳过

**Patterns to follow**: `_run_full_session_recomposition` line 1629 — `getattr(self._orchestrator, "_fs", None)` 获取 fs 实例；`cortex_fs.rm()` (line 162) — 递归删除接口。

**Test scenarios**:
- 删除带有效 URI 的 immediates → Qdrant 和 CortexFS 均被清理
- CortexFS rm 失败 → 不阻塞，Qdrant 仍被删除
- 无 CortexFS 实例 → graceful skip

**Verification**: merge 后检查被 supersede 的 URI 在 CortexFS 中不再存在。

## Dependencies and Sequencing

```
Unit 1 (raw content fix) → Unit 2 (anchor clustering)
                         → Unit 3 (CortexFS cleanup)
```

Unit 2 依赖 Unit 1：anchor-based 聚类需要 content.md 中是原文才有意义（摘要的 anchor 会变形）。Unit 3 独立，可与 Unit 2 并行，但逻辑上建议 Unit 1 → 3 → 2 以确保每步可验证。

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| CortexFS L2 读取增加 merge 延迟 | Medium | Low | 批量 `asyncio.gather` 并行读；失败 fallback 到 Qdrant |
| Jaccard 阈值不够好，切分过碎或过粗 | Medium | Medium | 可调参数，先用 0.15 + hard caps 保底；benchmark 验证 |
| `_build_recomposition_entries` 从 sync→async 影响调用链 | Low | Low | 仅 `_merge_buffer` 一处调用，加 `await` 即可 |
| CortexFS rm 对仍在被读取的目录 | Low | Low | fire-and-forget + warning log |

## Deferred to Implementation

- Jaccard 阈值 `0.15` 是否最优 — benchmark 验证后可调
- `_RECOMPOSE_SEGMENT_MAX_TOKENS` 值 3000 vs 当前 `_SEGMENT_MAX_TOKENS` 1200 — full_recompose 可用更大限制因为是后台任务
- 是否需要对 online merge 也做 anchor-based 切分（当前决策：不做，保持低延迟）
