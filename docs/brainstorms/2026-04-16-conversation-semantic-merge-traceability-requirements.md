---
date: 2026-04-16
topic: conversation-semantic-merge-traceability
---

# Conversation Semantic Merge + Traceability

## Problem Frame

OpenCortex 当前的对话合并路径会把一个 merge buffer 里的整段消息直接拼成一个 merged leaf。这个做法简单，但在长对话场景里会产生两个持续问题：

- 检索粒度过粗。一个 leaf 会混入过多主题、实体、时间和关系，导致 anchor 不够干净，probe 和排序容易长期停留在少数几条大 memory 上。
- 原文溯源不稳。merge 之后立即消息会被清理，最终 durable 记录如果只剩大块摘要，命中结果虽然能“约等于”覆盖原始对话，但很难稳定回到完整原文上下文。

这在 LoCoMo conversation 评测里已经表现得比较明显：

- 召回通常不是完全丢失，而是正确内容经常进 `top3` 但不是 `top1`
- 返回结果长期偏向少量大 leaf
- anchors 过宽，回答容易泛化

这次需求的目标不是推翻对话模式，也不是引入复杂知识图或多级树，而是把 merged memory 从“整块拼接”改成“顺序语义分段”，同时保留稳定原文溯源能力。

## Requirements

**Semantic Merge Shape**
- R1. 对话模式的 merged memory 不得再默认把整个 merge snapshot 压成单条 leaf。
- R2. merge 阶段必须在原始消息顺序上做顺序分段，并把一个 snapshot 拆成多条语义更聚焦的 leaf。
- R3. 分段必须保持消息原顺序，不得做全局重排、跨窗口重组或复杂聚类。
- R4. 分段规则必须基于少量、可解释、可调试的信号，优先考虑时间锚点变化、主题或 anchor 重叠下降、以及单段体量上限。
- R5. 每个分段产出的 leaf 必须保持足够聚焦，使其 abstract、overview 和 anchors 能代表该段的局部语义，而不是整段会话的混合摘要。

**Traceability**
- R6. 系统必须保留稳定的原文溯源能力，不能依赖已删除的 immediate records 才能还原证据链。
- R7. 每个 merged leaf 必须保留精确来源边界，至少能够表达它覆盖的消息范围。
- R8. 每个 merged leaf 必须能关联到一份长期可读的原文 transcript 来源，而不是只保留本地摘要结果。
- R9. 命中 merged leaf 后，系统必须能够明确回答“它来自哪段原文”和“它在完整会话中的位置”。
- R10. 原文溯源记录的存在不能削弱 merged leaf 本身的检索可用性；检索粒度和溯源能力必须并存，而不是二选一。

**Retrieval Quality**
- R11. probe 和 prepare 检索应优先命中更细粒度的 merged leaf，而不是长期停留在极少数超大 leaf 上。
- R12. 分段后的 anchors 必须比当前整块合并方式更精确，减少无关实体、时间和主题互相污染。
- R13. 在 conversation 场景下，正确证据进入 `top1` 的概率应明显优于当前整块合并形态，而不是只维持 `top3` 命中。
- R14. 新方案不得要求新增一条独立检索链路去专门查 transcript；正常 recall 仍然以 merged leaf 为主。

**Simplicity Constraints**
- R15. v1 不引入复杂层级树、图传播、跨段聚类器、额外 fallback 通道或新的检索模式。
- R16. v1 不要求 planner、probe、executor 重写成另一套协议；应尽量复用现有 conversation commit / end / prepare 生命周期。
- R17. transcript 记录主要服务溯源，不承担主排序职责；主检索对象仍然是语义分段后的 merged leaf。
- R18. trace 和调试信息必须能解释每个 merged leaf 的来源范围和分段结果，避免新的黑盒。

## Success Criteria

- 长对话 session 不再被压缩成极少数超大 merged leaf。
- `src/opencortex/context/manager.py` 的 merge 输出能生成多条局部语义聚焦的 leaf，并保持稳定的消息顺序边界。
- 命中 leaf 时可以稳定定位到完整 transcript 及其对应消息范围。
- conversation 检索结果中的 anchors 明显更聚焦，减少“整段会话主题大杂烩”现象。
- LoCoMo conversation 一类评测中，`top1` 质量优于当前整块合并方案，且回答泛化现象下降。
- 方案仍然保持简单，planning 不需要为 v1 发明新层级系统或新检索通道。

## Scope Boundaries

- In scope: conversation merge 形态、分段边界、leaf 级来源元数据、长期 transcript 溯源关联、相关 trace 可观测性。
- Out of scope: 新的 fallback 机制、复杂层级 memory tree、图式关系传播、额外 transcript 检索通道、重做 planner/probe 总体架构。
- Out of scope: 让 transcript 自身替代 merged leaf 成为主 recall 对象。
- Out of scope: 面向所有 memory 类型的一次性统一重构；本轮聚焦 conversation merge 路径。

## Key Decisions

- **检索粒度和溯源能力拆开**: merged leaf 负责 recall 精度，transcript 负责完整原文回看。
- **顺序分段优先于复杂聚类**: v1 只做顺序语义切段，不上难解释、难调试的聚类算法。
- **leaf 仍是主检索对象**: transcript 是来源底座，不是新的主搜索入口。
- **来源边界必须显式化**: `msg_range` 一类范围信息从“有最好”提升为稳定契约。
- **保留简单控制流**: 仍沿用 `commit -> merge -> end -> prepare` 主路径，不额外创造旁路。

## Dependencies / Assumptions

- `src/opencortex/context/manager.py` 已经维护 `msg_range`、buffer snapshot 和 merge 生命周期，具备承载顺序分段的基础。
- 当前系统已经能在 record 元数据中保存时间、实体、topics 和范围信息，说明 leaf 来源边界可以继续沿用现有元数据承载。
- transcript 相关能力在系统设计中已是既有概念，因此为 merged leaf 增加稳定 transcript 关联属于收敛既有能力，而不是引入全新产品概念。

## Outstanding Questions

### Resolve Before Planning

None.

### Deferred to Planning

- [Affects R4][Technical] v1 最小分段规则应该采用哪些具体阈值，才能避免切得过碎或继续过粗。
- [Affects R8][Technical] transcript 应该复用现有会话原文对象，还是新增一个更明确的 conversation source 记录形态。
- [Affects R11][Needs research] 需要怎样的 benchmark 样本和 trace 指标，才能稳定证明“少量超大 leaf”问题已经缓解。
- [Affects R18][Technical] trace 输出应以 leaf 级 segment 标识、范围摘要，还是 transcript 引用信息作为主要调试视角。

## Next Steps

-> /ce:plan for structured implementation planning
