---
date: 2026-04-16
topic: minimum-cost-retrieval-with-fact-points
supersedes:
  - docs/brainstorms/2026-04-16-layered-scope-first-retrieval-mainline-requirements.md
---

# Minimum-Cost Retrieval with Fact Points

## Problem Frame

Plan 005（overview-first + hard anchors）和 Plan 003（write-time anchor distill）已落地，但 recall 质量仍然不足，核心表现是**完全漏召**——相关记忆不出现在候选集中。

根因诊断确认问题不在排序层（rerank），而在候选集生成层：

1. **anchor_projection 语义丰富度不足**：当前 anchor 是单个术语（"Alice"、"Hangzhou"），不是原子事实句（"Alice moved to Hangzhou on May 1"）。单术语向量信息量太低，无法在向量搜索中可靠命中。
2. **anchor_projection 当前无向量嵌入**：`_anchor_projection_records()` 写入时不生成 embedding，Qdrant 中存储的是零向量。当前 probe 的 anchor 搜索走的是 `anchor_hits` 字段的文本匹配，不是向量搜索。这意味着 anchor 层当前无法被语义相似度搜索命中，只能靠精确/近似文本匹配。
3. **probe 窄搜索**：当前 probe top_k = 10-20，一旦漏了就无法补救。需要更宽的初始候选窗口。

注意：`ConeScorer`（`retrieve/cone_scorer.py`）已经实现了 minimum-cost 路径打分（`DIRECT_HIT_PENALTY = 0.3`、`HOP_COST = 0.05`、`min(paths)`）。本方案的检索改进是**扩展现有 ConeScorer 加入 fact_point 作为新的 tip 层**，而不是从头实现新算法。

对照外部参考实现后，明确了两个借鉴方向：

- **OpenViking**：三层 FS 存储结构（L0 abstract → L1 overview → L2 content）+ URI 路径层级的 scope-first 检索入口。已在 OpenCortex 中实现（CortexFS）。
- **m-flow**：分层锚点模型（Facet + FacetPoint）+ 倒锥形代价传播检索（minimum-not-average）。m-flow 的核心精髓是：进入正确语义邻域后，靠短硬锚点和原子事实句在 tip 层命中，然后通过代价最小化链路回到 Episode（leaf）。

本文档定义的方案结合两者精髓，在 OpenCortex 现有 Qdrant 单集合架构上实现：

- **存储**：保留 OpenViking 式三层 FS（L0/L1/L2），新增 m-flow 式 fact_point 层
- **写时**：两层策略——immediate 确定性 anchor，merge/recomposition 时 LLM 生成 fact_point
- **检索**：L0/L1 scope 划分 → 多层并行向量搜索 → minimum-cost 路径打分 → leaf 验证

## Core Concept: Minimum-Cost Path Scoring

传统 score fusion 把所有信号加权平均，要求多信号同时好。Minimum-cost 原则不同：

**一条强证据链就够了。**

对每个候选 leaf，枚举所有可能的命中路径，取代价最小的那一条作为最终得分：

```
Path 1: leaf 直接命中           cost = leaf_distance + 0.30 (direct penalty)
Path 2: anchor 命中 → leaf      cost = anchor_distance + 0.05 (hop)
Path 3: fact_point → anchor → leaf  cost = point_distance + 0.05 + 0.05 (two hops)
```

`final_cost = min(all_paths)` — 越小越好。

直接 leaf 命中有 penalty（0.30），因为宽泛 overview 匹配不如精确锚点匹配可信。这鼓励系统通过 anchor/fact_point 层命中，而不是依赖模糊语义相似度。

## Terms

- **fact_point**: 原子事实句柄，对应 m-flow FacetPoint。短、独立、可向量化的事实陈述，例如 "确定使用分批迁移避免停机"。区别于当前 anchor_projection 的单术语（"数据库迁移"）。
- **anchor**: 中层锚点，对应 m-flow Facet。短硬把手（实体名、时间点、模块名），当前 anchor_projection 即此角色。
- **leaf**: 最终 recall 落点，对应 m-flow Episode。当前 merged leaf / memory object。
- **path cost**: 从命中点到 leaf 的累积代价（向量距离 + 跳数惩罚）。
- **minimum path**: 到达某个 leaf 的所有路径中代价最小的那一条。
- **direct penalty**: 直接 leaf 命中的额外惩罚，鼓励通过更精确的 anchor/fact_point 路径命中。

## Requirements

### Fact Point Layer (Write-Time)

- R1. 每个 durable leaf 在 merge/recomposition 时必须生成 0-8 个 fact_point 记录，每个是一句独立的、可向量化的原子事实陈述。
- R2. fact_point 的 `search_text` 必须短（<80 字符）、具体（包含实体/数字/时间/路径等具体信号）、独立（脱离上下文仍可理解）。
- R3. fact_point 必须通过 m-flow 式质量门控：拒绝泛词、段落式文本、无具体锚点的短文本。
- R4. fact_point 必须作为独立 Qdrant 记录存储，携带 `retrieval_surface: "fact_point"`、嵌入向量、以及 `meta.projection_target_uri` 直接指向源 leaf URI（不经过 anchor 中转，以避免多跳查找开销）。fact_point 同时在 `meta.source_anchor_uri` 中记录其关联的 anchor URI，用于 trace 解释，但路径代价计算直接从 fact_point 到 leaf（单跳）。
- R4a. fact_point 和 anchor_projection 等派生检索面必须继承源 leaf 的全部访问控制字段（`scope`、`tenant_id`、`user_id`、`project_id`、`session_id`、`source_doc_id`），使其在 scoped query 和租户隔离下与源 leaf 具有相同可见性。检索投射回 leaf 后，必须再次校验目标 leaf 的可见性，丢弃已失去访问权限的路径。
- R5. fact_point 在 immediate 写入阶段不生成（无 LLM），仅在 merge/recomposition 路径中由 LLM 提取。
- R6. [次要范围] fact_point 应通过语义去重（基于向量相似度阈值 >= 0.90），避免近义事实占用有限 slot。此条对修复漏召无直接作用，属于存储质量优化，可在核心功能验证后实现。
- R7. 当 LLM 不可用时，leaf 仍然可以只有 anchor 层而没有 fact_point 层，系统退化为当前 anchor-first 行为而不是报错。

### Anchor Layer Enhancement (Write-Time)

- R8. anchor（当前 anchor_projection）保持当前角色：短硬把手，实体名/时间点/模块名/操作名。
- R9. anchor 的 `overview` 字段应从单术语升级为短语或短句（<40 字符），提供比单词更多的语义信号用于向量匹配。例如从 "Alice" 升级为 "Alice relocated to Hangzhou"。anchor 写入时必须生成嵌入向量（当前 anchor_projection 写入零向量，需修复）。
- R10. fact_point 通过 `meta.projection_target_uri` 直接链接到源 leaf，同时通过 `meta.source_anchor_uri` 记录关联 anchor。一个 leaf 下可有 0-8 个 fact_point。
- R11. anchor 质量门控保持当前标准（拒绝泛词、段落式、无具体信号），并新增最短长度要求（>= 4 字符）。

### Minimum-Cost Retrieval

- R12. 默认检索主路径必须扩展现有 `ConeScorer` 的 minimum-cost 路径打分，加入 fact_point 作为新的 tip 层，并将其提升为主打分机制，逐步取代 `_score_object_record()` 中的 score fusion 加权平均。
- R13. 检索第一步仍然是 L0/L1 scope 划分（当前 probe scope selection），在选定 scope 内执行后续步骤。
- R14. scope 确定后，必须对三个 retrieval_surface 分别做向量搜索：`fact_point`、`anchor_projection`、`l0_object`（当前 leaf 的 surface 名称），各取 top-k 候选。三层搜索在 `orchestrator._execute_object_query()` 中实现（不在 probe 中）。
- R15. 对每个候选 leaf，枚举所有可能的命中路径（直接 leaf / anchor→leaf / fact_point→leaf），取最小代价。fact_point 直接链接到 leaf（单跳），anchor 直接链接到 leaf（单跳），无需多跳图遍历。
- R16. 直接 leaf 命中必须有额外 penalty（建议 0.25-0.35），鼓励通过更精确的 anchor/fact_point 路径命中。
- R17. 每跳（hop）有固定小代价（建议 0.04-0.06），防止过长路径。
- R18. fact_point 命中距离 < 0.10 时，可信度足够高，跳数代价可打折（conditional discount，借鉴 m-flow 对近完美匹配的处理）。
- R19. 最终排序按 path cost 升序（越小越好）。可以在 path cost 基础上叠加 reward_score 和 active_count 作为微调信号。

### Wide Search Window

- R20. 三层搜索的初始候选窗口必须比当前 probe top_k 更宽。建议：fact_point top-60, anchor top-40, leaf top-20。
- R21. 候选合并后通过 URI 链接（`meta.projection_target_uri`）投射到 leaf 集合，最终按 minimum path cost 排序取 top-k。
- R22. 宽搜索只在 scope 内执行，不重新打开 cross-scope 搜索。

### Two-Layer Write Strategy (Conversation)

- R23. immediate 写入（`_write_immediate()`）保持当前行为：确定性 anchor 提取，无 LLM，无 fact_point。即时可搜索性通过 anchor_projection 保证。
- R24. merge/recomposition 写入时，LLM 在生成 overview + anchor_handles 的同时，提取 fact_point candidates。
- R25. fact_point 和 anchor_projection 的生命周期跟随源 leaf：源 leaf 被 supersede/删除时，其 fact_point 和 anchor_projection 一并清理。派生记录的创建和删除必须按可重试、可替换的集合语义执行——先写入新集合，成功后再删除旧集合，半失败时旧集合仍然可用。检索时若 `projection_target_uri` 指向不存在或已失去可见性的 leaf，必须丢弃该路径并在 trace 中标记为 `orphan_discarded`。

### Probe Gate & Scope Selection

- R26. scope 选择保持当前 probe 的 single-bucket 行为，不因新检索算法而改变。
- R27. explicit scope miss 仍然返回 scoped miss，不因新检索算法而静默 widening。
- R28. scope 选择完成后的所有搜索（fact_point/anchor/leaf）都在该 scope 内执行。
- R28a. 移除 `should_recall` 门控。当前 probe 返回 `should_recall=false` 时 planner 直接跳过检索（`planner.py:108`），三层搜索永远不被执行。参照 OpenViking 的做法——OpenViking 的 `HierarchicalRetriever` 无条件执行检索，不存在 should_recall 前置判断。默认检索链路应始终执行三层搜索，由 minimum-cost 打分和结果为空来自然表达"无可召回内容"，而不是由 probe 提前中断。`recall_mode="never"` 仍然保留作为调用方显式跳过检索的手段。

### Compatibility & Migration

- R29. 不引入图数据库。所有 anchor → leaf、fact_point → anchor 的关联通过 Qdrant 元数据字段（`meta.projection_target_uri`）表达。
- R30. 不引入多 Qdrant collection。三层搜索通过 `retrieval_surface` metadata filter 在单集合内实现。
- R31. 已有不含 fact_point 的历史数据仍然可以被检索。系统对没有 fact_point 的 leaf 退化为 anchor → leaf 两层路径或直接 leaf 命中，不报错。
- R32. 不引入 retrieval-time LLM（不做 HyDE / query rewrite）。
- R33. 不引入 fallback ladder 或双轨检索协议。

### Trace & Explainability

- R34. trace 必须暴露 minimum path 的来源：是 fact_point 路径、anchor 路径、还是直接 leaf 命中。
- R35. trace 必须暴露每条路径的代价构成：node_distance + hop_costs。
- R36. 保留 leaf 级原文溯源（`msg_range`、`source_uri`、recomposition stage）。

## Scope Boundaries

- In scope:
  - fact_point 数据模型、生成、质量门控、存储
  - anchor 短语化升级（从单术语到短句）+ 嵌入向量修复
  - 扩展现有 ConeScorer 加入 fact_point tip 层，提升为主打分机制
  - 三层并行向量搜索（在 orchestrator 执行路径中）+ URI 链接投射
  - 宽搜索窗口配置
  - LLM 层提取 prompt 扩展
  - conversation 两层写入策略对齐
  - trace 扩展
- 次要范围（核心验证后实现）：
  - fact_point 语义去重（R6）

- Out of scope:
  - 引入图数据库（Kuzu/Neo4j）
  - 引入多 Qdrant collection
  - m-flow 的 adaptive confidence（per-query 动态调权）
  - m-flow 的 edge semantic scoring（边语义打分，需要图数据库）
  - 改变 probe scope selection 机制（但 probe 的 should_recall 门控需要纳入 fact_point 信号，见 R28a）
  - 改变 conversation recomposition 机制
  - skill 纳入统一检索主链路

## Key Decisions

- **不引入图数据库**：用 Qdrant 元数据 URI 链接模拟 m-flow 的 Episode→Facet→FacetPoint 拓扑。丢失边语义打分，但保持基础设施简单。替代方案：用 fact_point 自身的匹配质量作为路径可信度指标，近完美匹配（distance < 0.10）视为强路径并享受 hop 折扣。
- **两层写入策略**：immediate 只做确定性 anchor，fact_point 延迟到 merge。和当前 conversation 两层写入一致（immediate 保证即时可搜索，merge 提升质量）。
- **扩展现有 ConeScorer 而非新建 scorer**：`ConeScorer` 已有 minimum-cost 路径打分基础设施，直接扩展加入 fact_point tip 层并提升为主打分，避免引入第二个并行打分器。
- **fact_point 直接链接到 leaf（单跳）**：fact_point 的 `projection_target_uri` 直接指向源 leaf URI，不经过 anchor 中转。这避免了多跳 Qdrant 查找，同时通过 `source_anchor_uri` 保留关联信息用于 trace。
- **anchor 写入时必须生成嵌入向量**：当前 anchor_projection 存储零向量，无法参与向量搜索。修复后 anchor 层才能真正作为中层检索面参与 minimum-cost 打分。
- **宽搜索窗口**：fact_point 60 + anchor 40 + leaf 20 = 120 初始候选，通过 URI 投射收敛到 leaf 集合。三层搜索通过 `asyncio.gather` 并行执行，scope filter 限制有效集合大小。预期延迟：scope 内并行搜索 ~40-60ms（当前单层 ~20ms），在可接受范围内。
- **anchor 短语化**：从单术语（"Alice"）升级为短句（"Alice relocated to Hangzhou"），提供更多向量匹配信号。这是对 m-flow Facet.search_text 的直接借鉴。

## Impact Analysis

### 受影响的代码路径

| 路径 | 影响 | 需求追溯 | 说明 |
|------|------|---------|------|
| `orchestrator._derive_layers()` | **修改** | R1,R2,R24 | LLM prompt 扩展：新增 fact_point 提取 |
| `orchestrator._anchor_projection_records()` | **修改** | R9 | anchor overview 短句化 + 写入时生成嵌入向量 |
| `orchestrator._sync_anchor_projection_records()` | **修改** | R4,R25 | 新增 fact_point 记录同步 + 生命周期清理 |
| `orchestrator.search()` / `_execute_object_query()` | **修改** | R14,R20 | 三层并行向量搜索（fact_point/anchor/leaf） |
| `retrieve/cone_scorer.py` | **扩展** | R12,R15-R19 | 扩展现有 min-cost scorer 加入 fact_point tip 层，提升为主打分机制 |
| `orchestrator._score_object_record()` | **逐步替代** | R12,R19 | 被扩展后的 cone_scorer min-cost 打分逐步替代 |
| `prompts.py` | **修改** | R1,R2 | layer derivation prompt 新增 fact_point 字段 |
| `storage/collection_schemas.py` | **修改** | R4 | 新增 fact_point retrieval_surface 索引 |
| `http/models.py` | **修改** | R34,R35 | trace 新增 path cost 字段 |
| `orchestrator._write_immediate()` | **不变** | R23 | 保持确定性 anchor，不生成 fact_point |

| `intent/probe.py` | **修改** | R28a | 移除 `should_recall` 门控，probe 仅负责 scope 选择，不再决定是否跳过检索 |
| `intent/planner.py` | **修改** | R28a | 移除 `recall_mode == "auto" and not should_recall` 短路逻辑 |

注意：`memory/mappers.py` 是读时投影模块，不含写时逻辑。fact_point 质量门控应在 `orchestrator.py` 中实现。

### 当前 cone_scorer 与新方案的关系

`ConeScorer` 已实现 minimum-cost 路径打分（`DIRECT_HIT_PENALTY=0.3`、`HOP_COST=0.05`、`min(paths)`），当前通过 EntityIndex 做实体关联。

新方案直接扩展 `ConeScorer`：
1. 加入 fact_point 作为新的 tip 层（比 entity anchor 更精确的命中面）
2. anchor_projection 向量化后也参与路径打分（当前只有文本匹配）
3. 扩展后的 scorer 成为主打分机制，逐步取代 `_score_object_record()` 中的 score fusion

不引入第二个并行打分器。`ConeScorer` 的现有接口和常量直接复用。

## Outstanding Questions

### Resolve Before Planning

无。

### Deferred to Planning

- fact_point 的 LLM 提取 prompt 具体格式和输出结构
- fact_point 语义去重的具体实现（内存中计算 vs 写入时查询 Qdrant）——此条为次要范围
- 三层搜索的具体 top-k 配置和调优策略
- ConeScorer 扩展的具体接口设计（从 per-record scorer 变为 set-level scorer，接收三层搜索结果集）
- anchor 嵌入向量生成的写路径延迟控制（0-6 个 anchor × embedding 调用）
- anchor 短语化的具体实现：是改 LLM prompt 让它生成短句，还是拼接 anchor_type + anchor_value
- fact_point 记录的 URI 命名方案
- `retrieval_surface` 命名确认：leaf 当前用 `l0_object`，是否重命名
- 移除 `should_recall` 后 probe 的精简职责边界（scope 选择 + evidence 收集，不做 recall 门控）
- 与当前 plan 005 已实现代码的具体合并策略

## Success Criteria

- 在固定 conversation benchmark slice 上，相比当前基线：
  - 正确证据出现在候选集中的比例显著提升（解决完全漏召）
  - 正确证据进入 top1 的比例提升
  - 正常热路径 latency 不因三层搜索而出现同量级恶化
- trace 可以直接区分命中来源是 fact_point 路径、anchor 路径、还是直接 leaf 命中
- 不含 fact_point 的历史数据仍然可被正常检索
- 系统在 LLM 不可用时退化为 anchor-first 行为而不报错

## Next Steps

→ /ce:plan for structured implementation planning
