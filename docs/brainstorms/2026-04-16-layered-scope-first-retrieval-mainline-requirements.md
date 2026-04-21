---
date: 2026-04-16
topic: layered-scope-first-retrieval-mainline
---

# Layered Scope-First Retrieval Mainline

## Problem Frame

OpenCortex 当前 retrieval 的主要问题，已经不只是某个 probe 或某个场景的局部 bug，而是主链路的控制顺序仍然不够稳定：

- 现有主路径仍然偏向“先搜一批叶子，再靠 planner / rerank / hydrate / cone 去补救”，而不是“先确定去哪找，再在局部范围内打准”。
- 现有 leaf 的 `abstract`、`overview`、`keywords`、`anchor_handles` 仍然主要是一次性摘要副产物，锚点不是第一公民，scope 也不是真正的一等检索控制面。
- conversation、memory、resource 这几类对象虽然开始共享同一 store 契约，但 retrieval 仍然没有共享一条真正统一、可解释、可扩展的主链路。

对照外部实现后，借鉴方向已经足够清楚：

- OpenViking 最值得借的不是完整 AGFS，也不是 session 壳摘要，而是三层 FS 背后的检索顺序：先用 `L0/L1` 做 scope 与目录裁决，再决定是否下钻到 `L2`。
- m-flow 最值得借的不是整套图路径打分，而是 `Facet / FacetPoint / hard handles` 这套思想：进入正确语义邻域后，靠短、硬、可匹配的锚点命中正确事实。

这轮不再把目标定义成“优化 conversation 检索”本身，而是把 conversation 视为首个高价值落地场景，收敛出一条未来适用于 `memory + resource/case/pattern` 的默认 retrieval 主链路：

- 用 OpenViking 的强项解决“先去哪找”
- 用 m-flow 的强项解决“进去后靠什么打准”
- 保留 OpenCortex 的 leaf 作为最终 recall 与 answer contract
- 保留 `msg_range` / source lineage 等原文溯源能力
- 不引入 fallback ladder、长期兼容层、复杂图传播或双轨长期共存

本轮明确不把 `skill` 纳入统一主链路。原因不是 `skill` 永远不需要分层，而是当前 `probe -> planner -> executor` 仍然主要是 memory-domain recall 主链路，先把可召回知识对象统一掉，才是最稳的推进顺序。

## Flow Shape

```text
query
  -> choose one active scope bucket and a tiny root set
  -> search L0/L1 scope surfaces
  -> enter selected semantic directories / clusters
  -> use hard anchors / points for first hit inside scope
  -> validate / rerank on leaf evidence
  -> assemble answer context from leaf results
```

## Terms

- `active scope bucket`: 当前 query 选中的单一 scope 强度层级，例如 `target_uri`、`session_id`、`source_doc_id`、context-type roots 或 tiny global discovery。一次正常检索只能激活一个 bucket。
- `root`: active scope bucket 内被保留的具体起点对象。一个 bucket 可以保留少量 roots，但这些 roots 仍属于同一 scope bucket。
- `L1 semantic directory`: root 下对检索可见的逻辑目录 / 语义簇，用 richer `overview` 承担目录级裁决。它可以是显式持久节点，也可以是等价的逻辑检索面，但无论采用哪种形态，都必须保留稳定 identity、稳定 leaf membership 和可追踪 parent relation，不能退化成一堆平铺 leaf。

## Requirements

**Logical FS Contract**
- R1. OpenCortex 的默认 retrieval 主链路必须建立在一套逻辑三层 FS 契约上，而不是继续把所有 durable records 平铺成同一种检索面。
- R2. `L0` 必须承担 scope 摘要与起点裁决职责；`L1` 必须承担逻辑目录 / 语义簇与 richer overview 职责；`L2` 必须承担 leaf 原文、最终命中与溯源职责。
- R3. 这套三层 FS 是逻辑层级，不要求复制 OpenViking 的物理 AGFS 或目录系统；它只要求 retrieval 能感知 `scope bucket / root / parent / level / child` 这套对象关系。
- R4. `L0/L1` 必须成为真实检索控制面，而不是只作为 leaf payload 里的被动字段存在。
- R5. 最终对外返回与排序的主体仍然是 `L2 leaf`，不是 `L0` scope 记录，也不是 `L1` 目录记录。
- R6. `L0/L1/L2` 主链路 contract 本轮定义覆盖 `memory + resource/case/pattern`；v1 rollout 先只落 conversation proving ground，后续其他类型按同一 contract 迁移；`skill` 不纳入这次统一 retrieval contract。

**Scope-First Retrieval**
- R7. 默认热路径必须先确定一个 active scope bucket，并在该 bucket 内收敛出少量 roots，然后再进入 anchors / points 命中阶段，而不是先做 broad leaf-first search。
- R8. scope 输入按强度排序：`target_uri` first，then `session_id`，then `source_doc_id`，then context-type roots，最后才允许 tiny global root discovery。
- R9. 一次正常检索只允许存在一个 active scope bucket；它可以在该 bucket 内保留少量 roots，并据此进入少量 `L1` 逻辑目录 / 语义簇，但不得把不同 bucket 的 roots 混成同一轮主搜索。
- R10. 当调用方提供显式 scope 且该 scope 无可用候选时，正常热路径对外必须返回 explicit scoped miss；它可以附带内部低置信诊断，但不得对外退化成模糊空结果，更不得静默 widening 到更弱 bucket。
- R11. 当没有显式 scope 时，系统可以执行 tiny global `L0/L1` root discovery，但这一步只负责找起点，不负责直接决定最终 leaf 命中。
- R12. probe trace 必须把 root discovery 与 leaf hit 区分开，避免再次出现“看起来有 scope，实际上还是叶子宽搜”的黑盒。

**Overview-First Layering**
- R13. 每个 durable `L2 leaf` 必须先生成 richer `overview`，再从该 `overview` 抽取 `abstract`；`abstract` 不再作为独立平行摘要生成。
- R14. 每个 `L1` 逻辑目录 / 语义簇也必须遵循同样的顺序：先形成 richer `overview`，再从 `overview` 中抽取更短 `abstract`，从而让 `L1` 真正可作为目录级检索面。
- R15. `overview` 必须优先保留对检索和回答最有区分度的局部事实，例如实体、事件、时间、地点、关系、决定或约束，而不是只给主题性概述。
- R16. `L0` 不要求承载完整语义，但它必须是 `L1 overview` 的稳定压缩结果，不能变成与 `L1` 平行漂移的另一条摘要线。
- R17. `L0/L1/L2` 的生成策略可以按对象类型不同而不同，但最终都必须落到同一层级契约上；如果某类对象不适合显式物化 `L1` 节点，也必须提供等价的 retrieval-visible `L1` surface，且该 surface 仍需具备稳定 identity、leaf membership 与 parent relation，而不是退回 flat leaf search。

**Hard Anchors And Points**
- R18. 进入选定 scope 后，系统必须有独立于 `overview` / `abstract` 的 hard-anchor 检索面，用于第一跳命中真正高区分度事实。
- R19. hard anchors 或 points 应优先表达专有名词、事件短语、时间点、数字、机构、地点、路径、模块名、操作名或其他短而硬的事实句柄。
- R20. hard anchors 或 points 必须过滤掉过泛标签、段落式文本、纯主题词和其他低区分度 handle；必要时允许写时改写坏 handle，使其变得更短、更硬、更可匹配。
- R21. hard anchors 是 additive retrieval surface，不得替换 canonical `overview`、`abstract`、`content` 或 traceability 元数据。
- R22. 正常热路径中，anchors / points 应承担 first-hit 职责；`overview` / `abstract` 负责局部候选确认、语义补充和回答上下文。
- R23. 命中 anchors / points 后，最终仍需回落到 leaf 进行验证、排序和上下文装配，而不是把 projection 结果直接暴露为最终 recall 对象。

**Conversation As The First Proving Ground**
- R24. conversation 模式是这条通用主链路的首个高价值落地场景，必须优先采用 ordered semantic segmentation + `overview -> abstract` + hard anchors 的完整契约。
- R25. conversation 切分必须保持原始消息顺序，并为每个 leaf 保留稳定来源范围，例如 `msg_range` 与 source lineage。
- R26. conversation durable merge 必须支持有界 tail recomposition；新消息进入后，允许最近 durable leaf 被重新切段、重新生成 `L1/L2` 表征与 anchors。
- R27. `session end` 后，conversation 必须异步执行一次整段 session 的全量语义重合并，用完整会话视角重建 leaf 边界、`L1/L2` 表征与 anchors。
- R28. `session end` 的全量重合并必须以替换该 session merged leaf 集合为目标，而不是叠加出第二套 competing leaves。
- R29. conversation 作为首个场景落地后，后续 memory 与 resource/case/pattern 需要逐步收敛到同一逻辑三层 FS；因此本次 brainstorm 定义的是统一最终 contract，而不是 conversation 私有分支。

**Planner And Executor Role**
- R30. planner 不再把“低置信”默认解释为 widening 许可；对显式 scope miss，对外契约直接是 scoped miss；对仍留在已选 scope 内的候选不足，低置信只用于决定是否继续在 scope 内验证、是否需要从 `L1` 升级到 `L2`。
- R31. executor 必须体现 `scope-first -> anchor/point first-hit -> leaf validation -> bounded assembly` 的控制顺序，而不是重新打开 broad leaf pool。
- R32. 如果保留 cone retrieval，它只能作为 second-order 局部扩展信号，在命中后围绕高质量 entity anchors 做局部补充；它不得重新承担默认第一跳职责。
- R33. 正常热路径不得重新引入 fallback ladder、额外 transcript 搜索通道或另一条并行检索协议来补救主表征质量问题。
- R34. 正常热路径不得依赖 retrieval-time HyDE、query rewrite 或其他额外 LLM round-trip 才能成立。

**Traceability And Simplicity**
- R35. trace 必须能解释本次检索选中了哪个 scope bucket、保留了哪些 roots、进入了哪些 `L1` 逻辑目录 / 语义簇、使用了哪些 anchors / points、最终为何由某个 leaf 胜出。
- R36. traceability 必须继续保留 leaf 级原文溯源能力，尤其是 `msg_range`、`source_uri`、recomposition stage 等能解释“答案来自哪里”的信息。
- R37. v1 不引入复杂 memory tree、全局图传播、多阶段仲裁矩阵、长期兼容层或双轨主链路。
- R38. v1 的目标不是复制 OpenViking 或 m-flow 的完整系统，而是把它们最强、最稳定、最低 carrying cost 的检索原则吸收到 OpenCortex 自己的默认主链路里。

## Success Criteria

- 默认热路径可以用一句话解释清楚：先找对 scope，再在 scope 内用 hard anchors / points 找对 leaf。
- `L0/L1` 真正参与 root discovery、目录裁决和下钻决策，而不是只作为 leaf 摘要附属字段。
- explicit scope 请求在 miss 时保持 explicit scoped miss，不再静默 widening，也不再以模糊 low-confidence 空结果对外呈现。
- 相近大主题下的不同事件、时间点和实体，能通过 anchors / points 在第一跳更稳定地区分开。
- 最终用户可见结果仍稳定落在 leaf 上，同时保留原文溯源能力。
- trace 可以直接区分是 scope bucket 选错、`L1` 目录 / 语义簇选错、anchor 不准，还是 leaf 边界不准。
- 在固定 conversation benchmark slice 与固定 query 集上，相对当前基线，正确证据进入 `top1` 的比例提升，且正常热路径 latency 不因本次主链路重排而出现同量级恶化。
- 这条主链路在 conversation 首次落地后，可以被自然扩展到 `memory + resource/case/pattern`，而不是继续新增平行检索协议。
- 方案保持简单，不需要 fallback ladder、新检索模式或复杂多级结构才能成立。

## Scope Boundaries

- In scope: `memory + resource/case/pattern` 的统一 retrieval contract 与 mainline 方向。
- In scope: 逻辑三层 FS、scope-first root discovery、overview-first layering、hard anchors / points、leaf 回落验证。
- In scope: conversation 作为 v1 首个落地场景的 ordered segmentation、tail recomposition、`session end` final recomposition。
- In scope: leaf 级 `msg_range` / source lineage 继续保留，用于答案溯源与 trace 解释。
- Out of scope: `skill` 纳入同一条主链路。
- Out of scope: 让 transcript 变成主检索对象。
- Out of scope: 复杂图传播、跨段聚类、全局 bundle scoring、额外 fallback 通道、多协议长期并存。
- Out of scope: 每次新消息进入都对整段 session 做全量重合并。
- Out of scope: 复制 OpenViking 的完整目录体系或 m-flow 的完整图检索体系。

## Key Decisions

- **Treat this as the mainline, not a local patch**: 目标不是修 conversation 特例，而是沉淀一条未来默认的 retrieval mainline，conversation 只是第一个 proving ground。
- **Contract now, rollout staged**: 这份 requirements 先定义统一最终 contract，但 v1 rollout 只先落 conversation proving ground，避免一次把全部对象类型同时吃下。
- **Logical FS, not physical AGFS**: 借的是 OpenViking 的三层检索顺序和父子边界，不是其完整物理文件系统；`L1` 可以是显式节点，也可以是等价的逻辑语义簇。
- **Path before anchor**: 先确定去哪找，再决定用哪个硬锚点打准；错误 scope 不能靠后续 anchors 低成本修复。
- **Overview first, abstract second**: `abstract` 必须从 richer `overview` 抽取，因为平行摘要更容易漂移。
- **Leaf remains the final contract**: `L0/L1` 与 anchors / points 都是内部检索控制面，外部 durable recall 单位仍然是 leaf。
- **Anchors own the first hit inside scope**: 进入正确语义邻域后，第一跳优先靠 hard anchors / points 打准。
- **Conversation proves the contract**: conversation 先落地，是因为它最容易暴露 leaf 表征、边界污染和 first-hit 失败问题。
- **Recomposition is allowed but bounded**: conversation 允许 leaf 被重组，但在线只做有界 tail recomposition，全局校正在 `session end` 异步完成。
- **Traceability is preserved end-to-end**: 引入逻辑三层 FS 不能牺牲 leaf 级原文溯源。
- **No fallback rescue path**: 如果主表征做不好，不靠额外 fallback ladder 补救；先把主路径做准。
- **Take the essence, not the whole systems**: 只吸收 OpenViking 和 m-flow 的黄金检索原则，不搬运其完整系统。

## Reference Implementations

这轮方案不是抽象地“参考 OpenViking 和 m-flow”，而是明确对照过它们的实际实现逻辑：

- OpenViking 的目录语义处理链路
  - 参考点：目录级语义先生成 richer `overview`，再从 `overview` 提取 `abstract`。
  - 本文档吸收点：OpenCortex 的 `L1 -> L0` 关系也应采用 `overview-first -> abstract-from-overview`，而不是让 `abstract` 与 `overview` 平行漂移。

- OpenViking 的分层递归检索链路
  - 参考点：全局搜索只负责 root discovery，真正主检索是 `L0/L1` 驱动的局部递归与下钻。
  - 本文档吸收点：OpenCortex 也应把全局搜索降级成起点发现器，而不是继续让它扮演主检索器。

- m-flow 的 Facet / FacetPoint 分层锚点模型
  - 参考点：进入正确语义邻域后，用 `search_text / anchor_text / point handles` 区分中层与细粒度检索面。
  - 本文档吸收点：OpenCortex 的 `L1` 与 leaf 内 anchors / points 也应明确分层，而不是继续维持 flat anchor list。

- m-flow 的 point refinement 链路
  - 参考点：对 bad handles 做过滤、必要时改写、去重，并优先保留更短、更硬、带 concrete anchors 的句柄。
  - 本文档吸收点：OpenCortex 的 hard anchors / points 也应有明确的写时治理，而不是把所有摘要副产物都当可用 anchor。

- `src/opencortex/orchestrator.py`
  - 当前对照点：`_derive_layers()` 现在一次同时生成 `abstract`、`overview`、`keywords`、`anchor_handles`，说明 anchors 仍是摘要链路的副产物。
  - 当前对照点：`_anchor_projection_records()` 已经提供独立 anchor projection 载体，但上游 anchor 质量仍未达到真正 hard-anchor 的标准。
  - 本文档吸收点：现有实现应从“并行摘要顺带产锚点”收敛到“overview-first + layered scope + dedicated hard anchors / points”。

- `src/opencortex/context/manager.py`
  - 当前对照点：conversation durable merge 已具备顺序语义切分基础。
  - 本文档吸收点：conversation 需要成为这套通用主链路的首个 proving ground，用 ordered segmentation 验证 `L1/L2` 表征质量与 leaf 边界重组。

- `src/opencortex/intent/probe.py`, `src/opencortex/intent/planner.py`, `src/opencortex/intent/executor.py`
  - 当前对照点：系统已经有 `probe -> planner -> executor` 契约，但 scope-first、`L0/L1` 裁决、anchor/point first-hit 还没有形成稳定主顺序。
  - 本文档吸收点：这条契约继续保留，但内部控制逻辑要重排到 layered scope-first mainline。

## Dependencies / Assumptions

- 当前系统已经存在 leaf 级 `abstract`、`overview`、`abstract_json`、anchor projection、`msg_range` 与 conversation merge 生命周期，说明这轮工作可以在现有契约上收敛，而不是从零发明新对象。
- 当前 `probe -> planner -> executor` 已经存在稳定边界，因此本轮更像控制顺序重排与 store surface 收敛，而不是新建一套 retrieval protocol。
- 当前 conversation 顺序语义切分已经具备基础骨架，可作为首个 proving ground。
- 假设当前 merge 生命周期允许对最近 durable tail 做替换式更新，否则 planning 需要先确认最小安全重合并写路径。
- 假设 `session end` 后允许异步执行一次 session 级替换式重写，否则 planning 需要先确认最终 leaf 集合的原子替换边界。
- 假设 `memory + resource/case/pattern` 最终都可落入同一层级契约；如果某一类对象天然缺少显式 `L1` 目录层，则 planning 需要定义其等价 `L1` retrieval surface 与最小 identity / membership 契约，而不是回退为 flat leaf 搜索。

## Outstanding Questions

### Resolve Before Planning

无。

### Deferred to Planning

- [Affects R8][Technical] tiny global root discovery 的最小结果上限与 bucket 选择规则应如何收敛。
- [Affects R18][Technical] 写时 bad handle 的拒绝、改写、去重规则，最小应做到多严格，才能避免泛词回流。
- [Affects R26][Technical] tail recomposition 的最小回看窗口应该如何定义，才能既允许旧 leaf 重组，又不把写入成本拉爆。
- [Affects R27][Technical] `session end` 异步全量重合并与在线 leaf 集合之间，最小可接受的替换与可见性契约应如何定义。
- [Affects R29][Needs research] memory 与 resource/case/pattern 这两类对象，哪些可以自然拥有显式 `L1` 目录层，哪些应以等价 `L1` surface 落地。
- [Affects R32][Technical] anchor-first 第一跳之后，cone 式局部扩展的触发阈值和适用查询类型应如何收敛。
- [Affects R35][Technical] trace 中哪些字段最值得暴露，才能让排查“是 scope 选错、anchor 不准、overview 不准，还是 leaf 边界不准”足够直接。

## Next Steps

-> /ce:plan for structured implementation planning
