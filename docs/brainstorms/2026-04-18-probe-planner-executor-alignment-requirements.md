---
date: 2026-04-18
topic: probe-planner-executor-alignment
supersedes:
  - docs/brainstorms/2026-04-16-layered-scope-first-retrieval-mainline-requirements.md
---

# Probe-Planner-Executor Alignment

## Problem Frame

OpenCortex 的 recall 主链路已经实现了 `probe -> planner -> executor` 三阶段架构（`src/opencortex/intent/`），但当前各阶段的职责边界偏离了原始设计意图：

1. **Probe 承担了过多决策**：当前 probe 包含 scope bucket 选择（`_select_scope_bucket()`）、starting point 发现（`_starting_point_probe()`）、双表面并行搜索（`_object_probe()` + `_anchor_probe()`）。原始设计中 probe 应该是一次向量搜索，只负责带出 URI 和 anchor 信号。

2. **Planner 不是真正的决策中心**：当前 planner 输出 retrieval_depth / search_profile / target_memory_kinds，但"沿 URI 递归搜索"和"围绕 anchor 扩散搜索"这两个核心检索策略不是 planner 的显式决策。scope 选择被 probe 抢走，深度决策被 executor 的 `arbitrate_hydration()` 二次修改。

3. **Executor 越权决策**：当前 executor 的 `arbitrate_hydration()` 在检索后重新判断是否从 L1 升级到 L2，这实质上是语义决策，应由 planner 预先决定。

原始设计的核心思想是：

```
Probe: 向量搜索 → URIs + Anchors（纯信号采集，不做决策）
Planner: 拿到信号 → 决定 scope + URI 递归 + anchor 扩散 + 深度（决策中心）
Executor: 忠实执行 planner 的配置对象（不重新判断）
```

本轮对齐的目标是把三阶段的职责收敛回这个原始设计。

注意：当前架构中 orchestrator（`build_scope_filter()` in `retrieval_support.py`）在调用 probe 之前已经注入了 scope filter（session_id / source_doc_id / target_uri）。本轮对齐将 scope **决策**移到 planner，但允许 orchestrator 继续将调用方传入的 scope context 作为 probe 的搜索约束——probe 使用这些约束缩小搜索范围，但不从中做出检索策略决策。

## Flow Shape

```text
query + caller_scope_context
  → Orchestrator: 将调用方 scope context 转为 probe 搜索约束
  → Probe: 在约束范围内执行向量搜索（不做策略决策）
    输出: { uris: [{uri, score, anchors, parent_uri, session_id, ...}],
            anchor_hits: [{anchor, source_uri, score}] }
  → Planner: 接收 probe 信号 + 调用方 scope context
    决策: { scope, drill_uris, expand_anchors, depth, budget }
    输出: 配置对象
  → Executor: 按配置执行（orchestrator 驱动实际存储操作）
    输出: bounded results
```

## Requirements

**Probe: Signal Collector（不做策略决策）**

- R1. Probe 的职责是在给定搜索约束范围内执行向量搜索，输出两类信号：URI 命中列表（含 score、parent_uri、session_id、source_doc_id、anchors 等元数据）和 anchor 命中列表（含 anchor 值、关联的源 URI、score）。Probe 可以接受 orchestrator 传入的 scope filter 作为搜索约束（缩小搜索范围），但不得从搜索结果中做出任何检索策略决策（scope bucket 选择、深度决策、scoped miss 判断等）。
- R2. Probe 不得包含 scope bucket 选择逻辑。当前 `_select_scope_bucket()` 中的 scope 决策必须移到 Planner。Orchestrator 可以将调用方传入的 scope context（session_id、source_doc_id 等）转为 probe 的搜索 filter，但这是搜索范围约束，不是策略决策。
- R3. Probe 不得包含 starting point 发现逻辑。当前 `_starting_point_probe()` 必须从 probe 中移除。Planner 从 probe 的 URI 命中列表元数据中推断 scope 归属。
- R4. Probe 输出的 URI 和 anchor 信号必须携带足够的元数据（session_id、source_doc_id、parent_uri、memory_kind、context_type），使 Planner 能据此做出 scope 和策略决策。当前 `SearchCandidate` 和 `StartingPoint` 已包含这些字段，但 probe 简化后只输出 `SearchCandidate` 级别的信号（不再有独立的 `StartingPoint` 类型）。
- R5. Probe 保留向量搜索缓存（LRU cache + TTL），避免相同 query 的重复嵌入和搜索。缓存 key 必须包含 orchestrator 传入的 scope filter，避免不同 session 的 probe 结果互相污染。
- R6. 当 probe 搜索失败或嵌入不可用时，输出空信号集合而不是抛异常，让 Planner 处理空证据场景。
- R6a. Probe 不得设置 `should_recall`、`scoped_miss` 等决策性字段。当前 probe 的 scoped miss 短路逻辑（`probe.py:264-268`）必须移到 Planner。

**Planner: Decision Center**

- R7. Planner 是三阶段中唯一的决策者。所有影响检索行为的决策——scope 选择、URI 递归、anchor 扩散、深度、预算——必须由 Planner 产出，不得分散在 Probe 或 Executor 中。
- R8. Planner 从 probe 输出中推断 scope：检查 URI 命中列表中的 session_id / source_doc_id / parent_uri 信号，结合调用方传入的 scope context（如 recall 时的 session_id），决定最终的 scope 约束。Scope 优先级维持不变：target_uri > session_id > source_doc_id > context_type > global。此处 "scope" 统一指 `ScopeLevel`（边界类型：SESSION_ONLY / DOCUMENT_ONLY / GLOBAL 等）和 `ProbeScopeSource`（决策来源：target_uri / session_id / global_root 等）两个维度。
- R9. Planner 决定 URI 递归策略：从 probe 命中的 URI 中选择哪些需要递归下钻（查找 children、siblings），以及下钻深度。
- R10. Planner 决定 anchor 扩散策略：从 probe 命中的 anchor 中选择哪些需要围绕扩散搜索，以及扩散半径。
- R11. Planner 预先决定检索深度（L0/L1/L2），不确定时默认 L2。Executor 的 `arbitrate_hydration()` 删除。
- R12. URI 递归和 anchor 扩散不是互斥的二选一——Planner 可以同时组合多种策略。输出是一个配置对象，描述所有需要执行的检索动作。
- R13. Planner 输出的配置对象至少包含：scope 约束（session_id / source_doc_id / target_uri 或 global）、drill_uris（需要递归的 URI 列表）、expand_anchors（需要扩散的 anchor 列表）、depth（L0/L1/L2）、budget（总候选上限）。
- R14. 当 probe 信号为空或 scope 内无候选时，Planner 必须输出显式的 scoped miss 决策，不得静默 widening 到更弱的 scope bucket。
- R15. Planner 保留当前的 coarse class 推断（LOOKUP/PROFILE/EXPLORE/RELATIONAL）作为内部启发式，但它是辅助策略选择的手段，不是对外暴露的决策维度。

**Executor: Faithful Execution**

- R16. Executor 接收 Planner 的配置对象，忠实执行，不做语义层面的重新判断。
- R17. Executor 将 Planner 的配置翻译成具体的存储操作（scope filter、URI 子节点查询、anchor 相关记录查询、L2 内容读取等）。注意：当前实际的存储操作由 orchestrator 的 `_execute_object_query()` 驱动，executor 只做 bind/finalize。本轮对齐保持这一分工——executor 产出配置，orchestrator 驱动执行。
- R18. Executor 保留硬性约束：最终返回结果数量不超过配置的 budget，原始候选数量不超过 raw_candidate_cap。
- R19. Executor 保留 degrade 能力：当执行超时或部分失败时，可以跳过可选步骤（rerank、cone expansion），但不得重新选择 scope 或修改深度。
- R20. Executor 的 `arbitrate_hydration()` 必须删除。深度决策在 Planner 阶段完成。

**Trace & Explainability**

- R21. 三阶段的 trace 必须保持清晰的职责归属：probe trace 只包含搜索信号；planner trace 包含所有决策及依据；executor trace 包含执行事实（延迟、候选数量、degrade 动作）。
- R22. 当检索失败时，trace 必须能区分是 probe 没找到信号、planner scope 选错、还是 executor 执行异常。

## Success Criteria

- Probe 代码中不包含任何 scope 选择、starting point 发现、或深度决策逻辑。
- Planner 代码中集中了所有决策：scope 选择 + URI 递归 + anchor 扩散 + 深度预决 + budget。
- Executor 代码中不包含 `arbitrate_hydration()` 或任何语义层面的重新判断。
- 三阶段的 trace 输出可以独立解释各自的职责：probe = 搜了什么、命中了什么；planner = 决定了什么、为什么；executor = 执行了什么、花了多久。
- 在固定 benchmark slice 上，对齐后的主链路质量不低于当前实现。
- 配置对象契约稳定后，可以独立测试 planner 的决策逻辑（给定 probe 输出 → 期望的配置对象），无需启动存储或执行完整检索。

## Scope Boundaries

- In scope: probe/planner/executor 职责边界重新划分。
- In scope: scope 选择从 probe 移到 planner。
- In scope: hydration 仲裁从 executor 移到 planner。
- In scope: planner 输出配置对象的契约定义。
- Out of scope: fact_point 层新增（Plan 006 单独推进）。
- Out of scope: minimum-cost path scoring（Plan 006 单独推进）。
- Out of scope: anchor 嵌入向量修复（Plan 006 单独推进）。
- Out of scope: conversation recomposition 机制变更。
- Out of scope: skill 纳入统一检索主链路。

## Key Decisions

- **Scope 决策移到 Planner**：Probe 不做 scope 选择。Orchestrator 可以把调用方的 scope context 作为搜索约束传给 probe（缩小范围），但 scope 的策略决策（选哪个 bucket、是否 scoped miss）由 Planner 做。
- **Planner 预决深度**：不确定时默认 L2。删除 executor 的 `arbitrate_hydration()`。
- **配置对象**：Planner 输出平铺配置（scope, drill_uris, expand_anchors, depth, budget），executor/orchestrator 按配置执行。
- **URI 递归和 anchor 扩散可组合**：不是二选一，planner 可同时组合。
- **Executor 是配置消费者，orchestrator 驱动存储操作**：保持现有分工，executor 做 bind/finalize，orchestrator 的 `_execute_object_query()` 负责实际 Qdrant 查询。

## Dependencies / Assumptions

- Orchestrator 的 `build_scope_filter()`（`retrieval_support.py`）在 probe 之前注入 scope filter。本轮对齐保留这一机制作为搜索约束，但 scope 策略决策移到 planner。
- Planner 预决深度时无法看到实际 L1 overview 内容，以保守策略（默认 L2）缓解。
- 内部重构，不改变 orchestrator 的公共接口（`search()` / recall / context prepare）。
- Executor 当前不执行存储操作（由 orchestrator 的 `_execute_object_query()` 驱动），本轮保持不变。

## Outstanding Questions

### Resolve Before Planning

无。

### Deferred to Planning

- [Affects R1][Technical] Probe 内部是单次还是两次并行 Qdrant query（object + anchor）？当前 anchor probe 是文本匹配不是向量搜索。
- [Affects R8][Technical] Planner 推断 scope 时是否需要额外 Qdrant 查询，还是纯粹基于 probe 返回的元数据。
- [Affects R13][Technical] 配置对象的 dataclass 定义，特别是 drill_uris 和 expand_anchors 的结构。
- [Affects R9][Needs research] URI 递归的实际查询方式：用 parent_uri filter 做子节点查询是否足够高效。
- [Affects R17][Technical] orchestrator 的 `_execute_object_query()` 如何消费 planner 新增的 drill_uris / expand_anchors 字段。

## Next Steps

-> /ce:plan for structured implementation planning
